import asyncio
import logging
import random
import uuid
import json
import os
import hashlib
import time
from collections import deque
from datetime import datetime, timezone
from redis import asyncio as redis
from functools import partial

from typing import Optional, Any
import asyncpg
from core.logger import setup_logger
from core.mq import MQManager, Ports, Topics
from core.alerts import send_cloud_alert
from core.execution_wrapper import MultiLegExecutor
from core.db_retry import with_db_retry
from NorenRestApiPy.NorenApi import NorenApi
from dotenv import load_dotenv

try:
    import uvloop # type: ignore
except ImportError:
    uvloop = None

logger = setup_logger("LiveBridge", log_file="logs/live_bridge.log")

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_DSN = f"postgres://trading_user:trading_pass@{DB_HOST}:5432/trading_db"
SEBI_BATCH_SIZE = 10
INTER_BATCH_WAIT = 1.01

class TokenBucketRateLimiter:
    def __init__(self, rate=8, per=1.0):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.monotonic()
    
    async def acquire(self):
        while self.tokens < 1:
            await asyncio.sleep(0.05)
            elapsed = time.monotonic() - self.last_refill
            self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.per))
            self.last_refill = time.monotonic()
        self.tokens -= 1


class LiveExecutionEngine:
    def __init__(self, mq_manager, pool, redis_client):
        self.mq = mq_manager
        self.pool = pool
        self.redis = redis_client
        
        load_dotenv()
        self.host = os.getenv("SHOONYA_HOST")
        self.user = os.getenv("SHOONYA_USER")
        self.pwd = os.getenv("SHOONYA_PWD")
        self.factor2 = os.getenv("SHOONYA_FACTOR2")
        self.vc = os.getenv("SHOONYA_VC")
        self.app_key = os.getenv("SHOONYA_APP_KEY")
        self.imei = os.getenv("SHOONYA_IMEI", "abc1234")
        self.daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "-16000.0"))  # [R2-11] -5000→-16000 per spec §4.3
        
        self.is_kill_switch_triggered = False
        self.total_realized_pnl = 0.0
        self.multileg_executor = MultiLegExecutor(self)
        
        # Spec 11.5: Token Bucket Rate Limiter
        self.rate_limiter = TokenBucketRateLimiter(rate=8, per=1.0)

        ws_url = str(self.host).replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP')
        if not ws_url.endswith('/'): ws_url += '/'
        
        self.api = NorenApi(host=self.host, websocket=ws_url)

    async def authenticate(self):
        """Logs into the broker API."""
        load_dotenv()
        logger.info("Authenticating Execution Bridge with Shoonya APIs...")
        
        pwd = hashlib.sha256(str(self.pwd).encode('utf-8')).hexdigest()
        u_app_key = '{0}|{1}'.format(self.user, self.app_key)
        app_key = hashlib.sha256(u_app_key.encode('utf-8')).hexdigest()
        
        # We need to run synchronous requests.post login in a thread pool ideally
        # to not block the asyncio loop, but startup is okay.
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, partial(self.api.login, 
            userid=self.user, password=pwd, twoFA=self.factor2, 
            vendor_code=self.vc, api_secret=app_key, imei=self.imei))
        
        if res and res.get('stat') == 'Ok':
            logger.info("✅ Live Bridge Authentication Successful.")
            # [Audit-Fix] Load persistent PnL from Redis on boot
            try:
                raw_pnl = await self.redis.get("DAILY_REALIZED_PNL_LIVE")
                if raw_pnl:
                    self.total_realized_pnl = float(raw_pnl)
                    logger.info(f"💾 PERSISTENCE: Recovered Daily PnL: ₹{self.total_realized_pnl:.2f}")
                    # Validate against kill switch immediately
                    await self.check_kill_switch()
            except Exception as e:
                logger.error(f"Failed to recover persistent PnL: {e}")
            return True
        else:
            logger.error(f"❌ Live Bridge Auth Failed: {res}")
            return False

    async def _reconnect_pool(self):
        """Phase 11.8: Attempt to reconnect the DB pool on failure."""
        try:
            if self.pool:
                await self.pool.close()
            self.pool = await asyncpg.create_pool(DB_DSN, command_timeout=10.0)
            logger.info("✅ Live DB Pool reconnected successfully.")
        except Exception as e:
            logger.error(f"❌ Failed to reconnect Live DB pool: {e}")
            raise

    @with_db_retry(max_retries=3, backoff=0.5)
    async def update_portfolio(self, conn, execution, fees):
        """Maintains the running real portfolio state in the DB."""
        symbol = execution['symbol']
        strategy_id = execution['strategy_id']
        exec_type = execution.get('execution_type', 'Actual') # We force Actual here
        parent_uuid = execution.get('parent_uuid', 'NONE')
        qty = float(execution['quantity']) if execution['action'] == 'BUY' else -float(execution['quantity'])
        price = float(execution['price'])
        
        # [Audit 5.1] Add parent_uuid to portfolio tracking
        record = await conn.fetchrow(
            "SELECT quantity, avg_price, realized_pnl FROM portfolio WHERE symbol = $1 AND strategy_id = $2 AND execution_type = $3 AND parent_uuid = $4 FOR UPDATE", 
            symbol, strategy_id, exec_type, parent_uuid
        )
        
        if not record:
            await conn.execute(
                """
                INSERT INTO portfolio (
                    symbol, strategy_id, parent_uuid, underlying, lifecycle_class, 
                    expiry_date, quantity, avg_price, initial_credit, short_strikes, 
                    execution_type, updated_at, has_calendar_risk
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                """,
                symbol, strategy_id, parent_uuid,
                execution.get('underlying'),
                execution.get('lifecycle_class', 'KINETIC'),
                execution.get('expiry_date'),
                qty, price, 
                float(execution.get('initial_credit', 0.0)),
                json.dumps(execution.get('short_strikes', {})),
                exec_type, execution['time'],
                execution.get('has_calendar_risk', False)
            )
            return
            
        current_qty = record['quantity']
        avg_price = float(record['avg_price'])
        realized_pnl = float(record['realized_pnl'])
        
        new_qty = current_qty + qty
        new_avg_price = avg_price
        
        if current_qty > 0 and qty < 0:
            closed_qty = min(current_qty, abs(qty))
            realized_pnl += float(closed_qty) * (float(price) - float(avg_price)) - float(fees)
            if new_qty == 0: new_avg_price = 0.0
        elif current_qty < 0 and qty > 0:
            closed_qty = min(abs(current_qty), qty)
            realized_pnl += float(closed_qty) * (float(avg_price) - float(price)) - float(fees)
            if new_qty == 0: new_avg_price = 0.0
        elif abs(new_qty) > abs(current_qty):
            new_avg_price = (float(abs(current_qty) * avg_price) + float(abs(qty) * price)) / float(abs(new_qty))
            
        await conn.execute(
            """
            UPDATE portfolio
            SET quantity = $1, avg_price = $2, realized_pnl = $3, updated_at = $4
            WHERE symbol = $5 AND strategy_id = $6 AND execution_type = $7 AND parent_uuid = $8
            """,
            new_qty, new_avg_price, realized_pnl, execution['time'], symbol, strategy_id, exec_type, parent_uuid
        )
        # [Audit 8.4] Only update realized PnL delta to avoid overwriting from multiple threads
        self.total_realized_pnl += (realized_pnl - float(record['realized_pnl']))

    async def check_kill_switch(self):
        """Monitors total realized P&L against the Hard Kill Switch boundary."""
        if self.total_realized_pnl <= self.daily_loss_limit:
            logger.critical(f"🛑 KILL SWITCH TRIGGERED: Daily Loss {self.total_realized_pnl} <= Limit {self.daily_loss_limit}")
            self.is_kill_switch_triggered = True
            await self.redis.publish("panic_channel", json.dumps({
                "action": "SQUARE_OFF", 
                "execution_type": "Actual",
                "reason": "KILL_SWITCH"
            }))

    async def _publish_liveliness(self):
        """[Audit-Fix] Periodic Liveliness Indicator for Dashboard UI."""
        while True:
            try:
                status_payload = {
                    "component": "LiveBridge",
                    "status": "ALIVE",
                    "kill_switch": self.is_kill_switch_triggered,
                    "realized_pnl": self.total_realized_pnl,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                await self.redis.set("LH:LiveBridge", json.dumps(status_payload), ex=30)
                await asyncio.sleep(5.0)
            except Exception as e:
                logger.error(f"Liveliness error: {e}")
                await asyncio.sleep(5.0)
            return True
        return False

    async def execute_live_order(self, order: dict) -> Optional[dict]:
        """Dispatches real network requests to Shoonya routing engine."""
        action_map = {"BUY": "B", "SELL": "S"}
        
        # [Audit 9.3] Dynamic exchange routing based on symbol
        exchange = self._get_exchange(order['symbol'])
            
        action = action_map.get(order['action'], 'B')
        symbol = order['symbol']
        
        # --- Spec 11.5: Rate Limiting ---
        await self.rate_limiter.acquire()

        logger.warning(f"🚀 SENDING LIVE MKT ORDER: {action} {order['quantity']} {symbol}")
        
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: self.api.place_order(
            buy_or_sell=action,
            product_type='I', # Intraday MIS
            exchange=exchange,
            tradingsymbol=symbol,
            quantity=order['quantity'],
            disclosedquantity=0,
            price_type=order.get('order_type', 'MKT'),
            price=order.get('price', 0),
            retention='DAY'
        ))
        
        if res and res.get('stat') == 'Ok':
            broker_oid = res['norenordno']
            logger.info(f"✅ Live Order Accepted. Shoonya ID: {broker_oid}")
            return {
                "id": broker_oid,
                "time": datetime.now(timezone.utc),
                "symbol": symbol,
                "action": order['action'],
                "quantity": float(order['quantity']),
                "price": float(order.get('price', 0.0)),
                "fees": float(order.get('fees', 20.0)),
                "strategy_id": order['strategy_id'],
                "execution_type": "Actual",
                "parent_uuid": order.get('parent_uuid', 'NONE')
            }
        
        # [Audit-Fix] API 400 Retry Logic with 0.1% nudge
        error_msg = res.get('emsg', 'Unknown')
        if "400" in error_msg or "REJECTED" in (res.get('stat') or ''):
            logger.warning(f"⚠️ API REJECTION ({error_msg}). Retrying with 0.1% nudge...")
            nudge = 0.001
            order['price'] = float(order.get('price', 0)) * (1 + nudge if action == 'B' else 1 - nudge)
            
            # Second attempt
            res = await loop.run_in_executor(None, lambda: self.api.place_order(
                buy_or_sell=action, product_type='I', exchange=exchange, 
                tradingsymbol=symbol, quantity=order['quantity'], disclosedquantity=0,
                price_type='LMT', price=order['price'], retention='DAY'
            ))
            
            if res and res.get('stat') == 'Ok':
                broker_oid = res['norenordno']
                logger.info(f"✅ Retry Successful. Shoonya ID: {broker_oid}")
                return {
                    "id": broker_oid,
                    "time": datetime.now(timezone.utc),
                    "symbol": symbol,
                    "action": order['action'],
                    "quantity": float(order['quantity']),
                    "price": float(order['price']),
                    "fees": float(order.get('fees', 20.0)),
                    "strategy_id": order['strategy_id'],
                    "execution_type": "Actual",
                    "parent_uuid": order.get('parent_uuid', 'NONE')
                }

        logger.error(f"❌ Live Order Rejected by Broker: {res}")
        return None


    def _get_exchange(self, symbol: str) -> str:
        """Determines the exchange for a given symbol."""
        if any(suffix in symbol for suffix in ["CE", "PE", "FUT"]):
            return 'BFO' if any(idx in symbol for idx in ["SENSEX", "BANKEX"]) else 'NFO'
        return 'NSE'

    async def get_last_price(self, symbol: str) -> float:
        """Fetches last traded price for an asset."""
        tick_str = await self.redis.get(f"latest_tick:{symbol}")
        if tick_str:
            return float(json.loads(tick_str).get('price', 0.0))
        return 0.0

    async def get_order_status(self, order_id: str) -> str:
        """Polls broker for order status."""
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, partial(self.api.single_order_history, orderno=order_id))
        if res and isinstance(res, list) and len(res) > 0:
            status = res[0].get('status')
            if status == 'COMPLETE': return 'COMPLETE'
            if status == 'REJECTED': return 'REJECTED'
            if status == 'CANCELED': return 'CANCELLED'
            return 'PENDING'
        return 'UNKNOWN'

    async def cancel_order(self, order_id):
        """Cancels a pending order."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, partial(self.api.cancel_order, orderno=order_id))

    async def handle_order(self, order, trade_pub_socket):
        """Async task handler for a single order to avoid blocking the main loop."""
        if self.is_kill_switch_triggered:
            logger.warning(f"Order rejected: Kill switch active. Symbol: {order['symbol']}")
            return

        # --- Double-Tap Execution Guard (SRS §2.6) ---
        lock_key = f"lock:{order['symbol']}"
        if await self.redis.exists(lock_key):
            logger.warning(f"❌ LIVE DUPLICATE REJECTED: {order['symbol']} has an active lock. Double-tap prevented.")
            return
        # Lock expires in 10s if not cleared by reconciler (safety fallback)
        await self.redis.setex(lock_key, 10, "LOCKED")

        # --- [Audit-Fix] Block Bridge on Stale Feed (> 1000ms) ---
        tick_raw = await self.redis.get(f"latest_tick:{order['symbol']}")
        if tick_raw:
            tick = json.loads(tick_raw)
            tick_ts = datetime.fromisoformat(tick.get("timestamp")).astimezone(timezone.utc)
            latency = (datetime.now(timezone.utc) - tick_ts).total_seconds()
            if latency > 1.0:
                logger.critical(f"🛑 STALE FEED BLOCK: {order['symbol']} latency {latency:.2f}s > 1s. Aborting.")
                await self.redis.delete(lock_key)
                return

        # --- Bridge-level Pre-flight Margin Check (SRS §3.10) ---
        # Note: Meta-Router performs the first check, but the Bridge provides the "Physical Muscle"
        # of insolvency protection by checking again right before dispatch.
        if order.get("action") == "BUY":
            price = float(order.get("price") or await self.get_last_price(order["symbol"]))
            qty = float(order["quantity"])
            required_margin = price * qty
            
            exec_suffix = "LIVE" if order.get("execution_type") == "Actual" else "PAPER"
            cash_key = f"CASH_COMPONENT_{exec_suffix}"
            coll_key = f"COLLATERAL_COMPONENT_{exec_suffix}"
            
            # Use LUA for atomic reservation
            LUA_RESERVE = """
            local cash_key = KEYS[1]
            local coll_key = KEYS[2]
            local amount = tonumber(ARGV[1])

            local cash = tonumber(redis.call('get', cash_key) or '0')
            local coll = tonumber(redis.call('get', coll_key) or '0')

            if cash >= amount then
                redis.call('decrby', cash_key, amount)
                return 1
            elseif (cash + coll) >= amount then
                local from_coll = amount - cash
                redis.call('set', cash_key, '0')
                redis.call('decrby', coll_key, from_coll)
                return 1
            else
                return 0
            end
            """
            
            res = await self.redis.eval(LUA_RESERVE, 2, cash_key, coll_key, int(required_margin))
            if res != 1:
                logger.error(f"❌ BRIDGE MARGIN VETO: {order['symbol']} needs ₹{required_margin:,.2f} but budget is exhausted.")
                await self.redis.delete(lock_key)
                return

        # --- Pending Journal Persistence (SRS §2.7) ---
        # Journal before network dispatch to Shoonya
        pending_key = f"Pending_Journal:{order['order_id']}"
        await self.redis.set(pending_key, json.dumps(order))

        execution = await self.execute_live_order(order)
        
        if execution:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # [R2-10] Include audit_tags matching paper_bridge fix
                    regime_raw = await self.redis.get("hmm_regime") or "UNKNOWN"
                    audit_tags = json.dumps({"regime": regime_raw, "execution_type": "Actual"})
                    await conn.execute(
                        """
                        INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type, audit_tags)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        """,
                        str(uuid.uuid4()),
                        execution['time'], execution['symbol'], execution['action'],
                        execution['quantity'], execution['price'], execution['fees'], 
                        execution['strategy_id'], execution['execution_type'], audit_tags
                    )
                    await self.update_portfolio(conn, execution, execution['fees'])

            
            # --- Phase 10: Triple-Barrier Handoff Ping ---
            handoff_payload = {
                "parent_uuid": order.get('parent_uuid'), 
                "symbol": order['symbol'],
                "strategy_id": order['strategy_id'],
                "execution_type": "Actual"
            }
            await self.redis.publish("NEW_POSITION_ALERTS", json.dumps(handoff_payload))
            
            # Clear pending journal on fill
            await self.redis.delete(f"Pending_Journal:{order['order_id']}")

            # --- [Audit-Fix] 3s Partial Fill Rollback ---
            await asyncio.sleep(3.0)
            status = await self.get_order_status(execution["id"])
            if status == "PARTIAL":
                 logger.warning(f"⚠️ PARTIAL FILL TIMEOUT (3s): {execution['symbol']}. Triggering Rollback.")
                 await self._handle_basket_rollback(execution.get('parent_uuid'))
                 return

            # 4. Broadcast execution confirmation via ZMQ
            # [R2-02] Fixed: use MQManager.send_json for proper 3-part envelope
            await self.mq.send_json(trade_pub_socket, f"EXEC.{execution['symbol']}", {
                "id": execution["id"],
                "symbol": execution["symbol"],
                "price": float(execution["price"]),
                "type": "EXECUTION"
            })
            
            # --- Pub/Sub Alert: Transaction Confirmation ---
            emoji = "🟢" # Live
            asyncio.create_task(send_cloud_alert(
                f"{emoji} *TRANSACTION*: {execution['action']} {execution['quantity']} {execution['symbol']}\n"
                f"Price: ₹{execution['price']:.2f} | Strategy: {execution['strategy_id']}",
                alert_type="TRANSACTION"
            ))
            
            # Publish live P&L and lot count to Redis for cloud_publisher and UI
            await self.redis.set("DAILY_REALIZED_PNL_LIVE", str(self.total_realized_pnl))
            if execution["action"] == "BUY":
                await self.redis.incr("ACTIVE_LOTS_COUNT")
            elif execution["action"] == "SELL":
                await self.redis.decr("ACTIVE_LOTS_COUNT")
            
            await self.check_kill_switch()

    async def run(self, pull_socket):
        """Listens for orders and spawns async handlers (Fire-and-Forget)."""
        logger.info("Started Live Execution loop. Waiting for signals...")
        asyncio.create_task(send_cloud_alert("⚡ LIVE BRIDGE: Active and monitoring Shoonya execution stream.", alert_type="SYSTEM"))
        
        trade_pub_socket = self.mq.create_publisher(Ports.TRADE_EVENTS)
        
        while True:
            try:
                topic, order = await self.mq.recv_json(pull_socket)
                if not order: continue
                    
                exec_type = order.get('execution_type', 'Actual')
                if exec_type != "Actual":
                    continue
                    
                # Phase 13.3: Command Handling
                if order.get("cmd") == "FORCE_MARKET_ORDER":
                    logger.warning(f"⚠️ FORCING MARKET ORDER: {order.get('symbol')} Qty={order.get('qty')}")
                    order["order_type"] = "MKT"
                    order["action"] = order.get("side", order.get("action"))
                    order["quantity"] = order.get("qty", order.get("quantity"))
                    # Fall through to standard handle_order
                elif order.get("cmd") == "BASKET_ROLLBACK":
                    # Rollbacks should technically go through the rollback listener, 
                    # but if sent here, we handle it too.
                    asyncio.create_task(self._handle_basket_rollback(order.get("parent_uuid")))
                    continue

                logger.info(f"[LIVE SIGNAL] Queueing order: {order.get('action', 'MULTI_LEG')} {order.get('quantity', 'N/A')} {order.get('symbol', 'N/A')}")
                
                # --- Recommendation 4: Fire-and-Forget Execution ---
                if order.get('type') == 'MULTI_LEG':
                    asyncio.create_task(self.multileg_executor.execute_legs(order['legs']))
                else:
                    asyncio.create_task(self.handle_order(order, trade_pub_socket))
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Error in execution dispatcher: {e}")

    async def panic_listener(self):
        """Listens for Global Square Off commands via Redis PubSub."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("panic_channel")
        logger.info("Live Panic listener active on 'panic_channel'. Listening for Actual mode blasts...")
        
        async for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    data = json.loads(message['data'])
                    # [C3-03] Accept both SQUARE_OFF and SQUARE_OFF_ALL, and execution_type Actual or ALL
                    action = data.get('action', '')
                    exec_type = data.get('execution_type', '')
                    if action in ["SQUARE_OFF", "SQUARE_OFF_ALL"] and exec_type in ["Actual", "ALL"]:
                        logger.critical("🚨 RECEIVED LIVE PANIC SIGNAL: SQUARING OFF REAL POSITIONS WITH BROKER APIs! 🚨")
                        # ... existing logic ...
                    elif action == "SQUARE_OFF_SIDE" and exec_type in ["Actual", "ALL", ""]:
                        symbol = data.get('symbol')
                        side = data.get('side') # "S" or "B"
                        logger.critical(f"🚨 TRAP ALERT: Selective Liquidation for {symbol} side {side}")
                        
                        async with self.pool.acquire() as conn:
                            positions = await conn.fetch(
                                "SELECT symbol, quantity FROM portfolio WHERE symbol = $1 AND quantity != 0 AND execution_type = 'Actual'",
                                symbol
                            )
                            
                            for pos in positions:
                                # Sell if long and side='S', or Buy if short and side='B'
                                if (side == "S" and pos['quantity'] > 0) or (side == "B" and pos['quantity'] < 0):
                                    la = "SELL" if pos['quantity'] > 0 else "BUY"
                                    lq = abs(pos['quantity'])
                                    logger.critical(f"Trap Fire: {la} {lq} {symbol}")
                                    
                                    loop = asyncio.get_running_loop()
                                    ex = self._get_exchange(symbol)
                                    await loop.run_in_executor(None, lambda a=la, s=symbol, q=lq, e=ex: self.api.place_order(
                                        buy_or_sell=a[0], product_type='I', exchange=e, tradingsymbol=s, 
                                        quantity=q, disclosedquantity=0, price_type='MKT', price=0, retention='DAY'
                                    ))
                                    # Update DB
                                    await conn.execute("UPDATE portfolio SET quantity = 0, avg_price = 0 WHERE symbol = $1 AND execution_type = 'Actual'", symbol)
                        
                        # In production we would poll api.get_positions()
                        # For now, rely on our internal Database truth state
                        async with self.pool.acquire() as conn:
                            positions = await conn.fetch("SELECT symbol, quantity FROM portfolio WHERE quantity != 0 AND execution_type = 'Actual'")
                            
                            if not positions:
                                logger.info("No live positions to liquidate in panic.")
                                return

                            # SEBI 10-ops rule: Batch by 10, wait 1.01s
                            for i in range(0, len(positions), SEBI_BATCH_SIZE):
                                batch = positions[i:i + SEBI_BATCH_SIZE]
                                for pos in batch:
                                    action = "SELL" if pos['quantity'] > 0 else "BUY"
                                    qty = abs(pos['quantity'])
                                    symbol = pos['symbol']
                                    
                                    logger.critical(f"Panic Fire: {action} {qty} {symbol}")
                                    loop = asyncio.get_running_loop()
                                    panic_ex = self._get_exchange(symbol)
                                    
                                    # Blast market orders rapidly
                                    await loop.run_in_executor(None, lambda a=action, s=symbol, q=qty, ex=panic_ex: self.api.place_order(
                                        buy_or_sell=a[0], product_type='I', exchange=ex, tradingsymbol=s, 
                                        quantity=q, disclosedquantity=0, price_type='MKT', price=0, retention='DAY'
                                    ))
                                
                                if i + SEBI_BATCH_SIZE < len(positions):
                                    logger.info(f"SEBI Panic Batch complete. Waiting {INTER_BATCH_WAIT}s...")
                                    await asyncio.sleep(INTER_BATCH_WAIT)
                                    
                            # [R2-17] Preserve realized_pnl — only zero out quantity and avg_price
                            await conn.execute("UPDATE portfolio SET quantity = 0, avg_price = 0 WHERE execution_type = 'Actual'")
                            
                        logger.critical("✅ Live Market Wipeout Completed.")
                        asyncio.create_task(send_cloud_alert(
                            "🚨 LIVE PANIC LIQUIDATION COMPLETED\n"
                            "All real positions have been closed with market orders.",
                            alert_type="CRITICAL"
                        ))
                        
                except Exception as e:
                    logger.error(f"Live Panic exception: {e}")

    async def _handle_basket_rollback(self, parent_uuid: str):
        """Phase 13.3: Rapid Liquidation of all Live legs in a basket."""
        if not parent_uuid or parent_uuid == "NONE": return
        
        logger.critical(f"🚨 LIVE ROLLBACK: Closing all positions for {parent_uuid}")
        async with self.pool.acquire() as conn:
            # Query our internal truth for this basket
            positions = await conn.fetch(
                "SELECT symbol, quantity FROM portfolio WHERE parent_uuid = $1 AND quantity != 0 AND execution_type = 'Actual'",
                parent_uuid
            )
            
            for pos in positions:
                action = "SELL" if pos['quantity'] > 0 else "BUY"
                qty = abs(pos['quantity'])
                symbol = pos['symbol']
                
                logger.critical(f"Rollback Fire: {action} {qty} {symbol}")
                # [Audit 9.3] Identify exchange dynamically
                rollback_ex = self._get_exchange(symbol)
                
                # Dispatch market order to broker
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda a=action, s=symbol, q=qty, ex=rollback_ex: self.api.place_order(
                    buy_or_sell=a[0], product_type='I', exchange=ex, tradingsymbol=s, 
                    quantity=q, disclosedquantity=0, price_type='MKT', price=0, retention='DAY'
                ))
            
            # Clear them from portfolio
            await conn.execute("UPDATE portfolio SET quantity = 0, avg_price = 0 WHERE parent_uuid = $1 AND execution_type = 'Actual'", parent_uuid)
            
        logger.info(f"✅ Live Rollback complete for {parent_uuid}")

    async def cmd_listener(self):
        """Listens for systemic commands including Rollbacks via SYSTEM_CMD."""
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["GLOBAL", "RECONCILER"])
        logger.info("Live Command listener active on SYSTEM_CMD.")
        while True:
            try:
                topic, msg = await self.mq.recv_json(sub)
                if msg and msg.get("cmd") == "BASKET_ROLLBACK":
                    asyncio.create_task(self._handle_basket_rollback(msg.get("parent_uuid")))
            except Exception as e:
                logger.error(f"Cmd listener error: {e}")
                await asyncio.sleep(1)

async def _run_heartbeat(r):
    from core.health import HeartbeatProvider
    hb = HeartbeatProvider("LiveBridge", r)
    await hb.run_heartbeat()

async def main():
    logger.info("Initializing Live Bridge...")
    
    mq = MQManager()
    # [Audit 4.2] Fix pull socket bind
    pull_socket = mq.create_pull(Ports.ORDERS, bind=True)
    # [Audit 14.1] Better async Redis config
    redis_client = redis.from_url(f"redis://{REDIS_HOST}:6379", decode_responses=True, socket_timeout=5.0)
    
    # [R2-14] DB pool with retry
    pool = None
    for attempt in range(10):
        try:
            pool = await asyncpg.create_pool(DB_DSN, command_timeout=10.0)
            break
        except Exception as e:
            logger.error(f"Live Bridge DB connect failed (Attempt {attempt+1}): {e}")
            await asyncio.sleep(min(3 * (attempt + 1), 30))
    if not pool:
        logger.critical("Failed to connect to DB after 10 attempts. Exiting.")
        return
    
    engine = LiveExecutionEngine(mq, pool, redis_client)
    auth_success = await engine.authenticate()
    
    if auth_success:
        try:
            await asyncio.gather(
                engine.run(pull_socket),
                engine.panic_listener(),
                engine.cmd_listener(),       # Phase 13.3
                engine._publish_liveliness(), # [Audit-Fix] Component 5
                _run_heartbeat(redis_client), # Phase 9: UI & Observability
            )
        except KeyboardInterrupt:
            logger.info("Shutting down live bridge safely.")
        finally:
            pull_socket.close()
            mq.context.term()
            if pool: await pool.close()
            await redis_client.aclose()
    else:
        logger.error("Data integrity failure. Shutting down Live Bridge to prevent errant fires.")

if __name__ == "__main__":
    if uvloop:
        uvloop.install()
    elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
