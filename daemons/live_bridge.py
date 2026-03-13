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

import asyncpg
import redis.asyncio as redis
from dotenv import load_dotenv
from core.mq import MQManager, Ports, Topics
from core.alerts import send_cloud_alert
from core.execution_wrapper import MultiLegExecutor
from NorenRestApiPy.NorenApi import NorenApi

try:
    import uvloop
except ImportError:
    uvloop = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("LiveBridge-Shoonya")

DB_DSN = "postgres://trading_user:trading_pass@localhost:5432/trading_db"

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
        self.daily_loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "-5000.0"))
        
        self.is_kill_switch_triggered = False
        self.total_realized_pnl = 0.0
        self.multileg_executor = MultiLegExecutor(self)
        self.order_timestamps = deque()         # Layer 1: 10 OPS token bucket
        self.minute_timestamps = deque()        # Layer 2: 190req/60s rolling window

        ws_url = self.host.replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP')
        if not ws_url.endswith('/'): ws_url += '/'
        
        self.api = NorenApi(host=self.host, websocket=ws_url)

    async def authenticate(self):
        """Logs into the broker API."""
        logger.info("Authenticating Execution Bridge with Shoonya APIs...")
        
        pwd = hashlib.sha256(self.pwd.encode('utf-8')).hexdigest()
        u_app_key = '{0}|{1}'.format(self.user, self.app_key)
        app_key = hashlib.sha256(u_app_key.encode('utf-8')).hexdigest()
        
        # We need to run synchronous requests.post login in a thread pool ideally
        # to not block the asyncio loop, but startup is okay.
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: self.api.login(
            userid=self.user, 
            password=pwd, 
            twoFA=self.factor2, 
            vendor_code=self.vc, 
            api_secret=app_key, 
            imei=self.imei
        ))
        
        if res and res.get('stat') == 'Ok':
            logger.info("✅ Live Bridge Authentication Successful.")
            return True
        else:
            logger.error(f"❌ Live Bridge Auth Failed: {res}")
            return False

    async def update_portfolio(self, conn, execution, fees):
        """Maintains the running real portfolio state in the DB."""
        symbol = execution['symbol']
        strategy_id = execution['strategy_id']
        exec_type = execution.get('execution_type', 'Actual') # We force Actual here
        qty = execution['quantity'] if execution['action'] == 'BUY' else -execution['quantity']
        price = execution['price']
        
        record = await conn.fetchrow("SELECT * FROM portfolio WHERE symbol = $1 AND strategy_id = $2 AND execution_type = $3 FOR UPDATE", symbol, strategy_id, exec_type)
        
        if not record:
            await conn.execute(
                """
                INSERT INTO portfolio (symbol, strategy_id, quantity, avg_price, execution_type, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                symbol, strategy_id, qty, price, exec_type, execution['time']
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
        elif current_qty >= 0 and qty > 0:
            new_avg_price = (float(current_qty * avg_price) + float(qty * price)) / float(new_qty)
        elif current_qty <= 0 and qty < 0:
            new_avg_price = (float(abs(current_qty) * avg_price) + float(abs(qty) * price)) / float(abs(new_qty))
            
        await conn.execute(
            """
            UPDATE portfolio
            SET quantity = $1, avg_price = $2, realized_pnl = $3, updated_at = $4
            WHERE symbol = $5 AND strategy_id = $6 AND execution_type = $7
            """,
            new_qty, new_avg_price, realized_pnl, execution['time'], symbol, strategy_id, exec_type
        )
        self.total_realized_pnl = realized_pnl # Update for kill switch check

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
            return True
        return False

    async def execute_live_order(self, order):
        """Dispatches real network requests to Shoonya routing engine."""
        action_map = {"BUY": "B", "SELL": "S"}
        
        # Assuming NSE Equities for the dummy symbols. In prod we extract exchange prefix from the symbol mapping.
        exchange = 'NSE'
        symbol = order['symbol']
        action = action_map.get(order['action'], 'B')
        
        # --- Dual-Layer Rate Limiting (SRS §5) ---
        # Layer 1: 10 OPS Token Bucket (SEBI Algo Rule)
        while True:
            now = time.time()
            while self.order_timestamps and self.order_timestamps[0] <= now - 1.0:
                self.order_timestamps.popleft()
            if len(self.order_timestamps) < 10:
                self.order_timestamps.append(now)
                break
            wait_time = self.order_timestamps[0] + 1.001 - now
            await asyncio.sleep(max(0, wait_time))

        # Layer 2: 190req/60s Rolling Window (prevents Shoonya 200/min lockout)
        while True:
            now = time.time()
            while self.minute_timestamps and self.minute_timestamps[0] <= now - 60.0:
                self.minute_timestamps.popleft()
            if len(self.minute_timestamps) < 190:
                self.minute_timestamps.append(now)
                break
            wait_time = self.minute_timestamps[0] + 60.001 - now
            logger.warning(f"Rate limiter L2: 190req/60s reached. Waiting {wait_time:.2f}s")
            await asyncio.sleep(max(0, wait_time))

        logger.warning(f"🚀 SENDING LIVE MKT ORDER: {action} {order['quantity']} {symbol}")
        
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, lambda: self.api.place_order(
            buy_or_sell=action,
            product_type='I', # Intraday MIS
            exchange=exchange,
            tradingsymbol=symbol,
            quantity=order['quantity'],
            disclosedquantity=0,
            price_type='MKT',
            price=0,
            retention='DAY'
        ))
        
        if res and res.get('stat') == 'Ok':
            broker_oid = res['norenordno']
            logger.info(f"✅ Live Order Accepted. Shoonya ID: {broker_oid}")

            # Update pending_orders with broker_order_id so reconciler can poll it
            local_oid = order.get('order_id')
            if local_oid:
                import json as _json
                existing_raw = await self.redis.hget('pending_orders', local_oid)
                if existing_raw:
                    existing = _json.loads(existing_raw)
                    existing['broker_order_id'] = broker_oid
                    await self.redis.hset('pending_orders', local_oid, _json.dumps(existing))

            tick_str = await self.redis.get(f"latest_tick:{symbol}")
            exec_price = float(order['price'])
            if tick_str:
                exec_price = float(json.loads(tick_str)['price'])

            return {
                "id": broker_oid,
                "time": datetime.now(timezone.utc),
                "symbol": symbol,
                "action": order['action'],
                "quantity": order['quantity'],
                "price": exec_price,
                "fees": 20.0,
                "strategy_id": order['strategy_id'],
                "execution_type": "Actual"
            }
        else:
            logger.error(f"❌ Live Order Rejected by Broker: {res}")
            return None

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

        # --- Pending Journal Persistence (SRS §2.7) ---
        # Journal before network dispatch to Shoonya
        pending_key = f"Pending_Journal:{order['order_id']}"
        await self.redis.set(pending_key, json.dumps(order))

        execution = await self.execute_live_order(order)
        
        if execution:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        uuid.UUID(int=hash(execution['id'])),
                        execution['time'], execution['symbol'], execution['action'],
                        execution['quantity'], execution['price'], execution['fees'], 
                        execution['strategy_id'], execution['execution_type']
                    )
                    await self.update_portfolio(conn, execution, execution['fees'])
            
            # --- Resilience: Clear Pending Journal ---
            # Order successfully written to DB and confirmed by broker execution state
            await self.redis.delete(pending_key)
            # lock:{symbol} is cleared by order_reconciler.py as per spec.
            
            }, topic=f"EXEC.{execution['symbol']}")
            
            # --- Pub/Sub Alert: Transaction Confirmation ---
            emoji = "🟢" # Live
            await send_cloud_alert(
                f"{emoji} *TRANSACTION*: {execution['action']} {execution['quantity']} {execution['symbol']}\n"
                f"Price: ₹{execution['price']:.2f} | Strategy: {execution['strategy_id']}",
                alert_type="TRANSACTION"
            )
            
            # Publish live P&L and lot count to Redis for cloud_publisher and UI
            await self.redis.set("DAILY_REALIZED_PNL_LIVE", str(self.total_realized_pnl))
            # Increment active lots count; decremented on SELL/CLOSE by reconciler
            if execution["action"] == "BUY":
                await self.redis.incr("ACTIVE_LOTS_COUNT")
            elif execution["action"] == "SELL":
                await self.redis.decr("ACTIVE_LOTS_COUNT")
            
            # Check kill switch after every trade execution
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
                    
                exec_type = order.get('execution_type', 'Paper')
                if exec_type != "Actual":
                    continue
                    
                logger.info(f"[LIVE SIGNAL] Queueing order: {order.get('action', 'MULTI_LEG')} {order.get('quantity', 'N/A')} {order.get('symbol', 'N/A')}")
                
                # --- Recommendation 4: Fire-and-Forget Execution ---
                # We don't 'await' the full handler, we fire a task
                if order.get('type') == 'MULTI_LEG':
                    # Recommendation 5: Multi-Leg Atomic Execution
                    asyncio.create_task(self.multileg_executor.execute_legs(order['legs']))
                else:
                    # Regular single order
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
                    if data.get('action') == "SQUARE_OFF" and data.get('execution_type') == "Actual":
                        logger.critical("🚨 RECEIVED LIVE PANIC SIGNAL: SQUARING OFF REAL POSITIONS WITH BROKER APIs! 🚨")
                        
                        # In production we would poll api.get_positions()
                        # For now, rely on our internal Database truth state
                        async with self.pool.acquire() as conn:
                            positions = await conn.fetch("SELECT symbol, quantity FROM portfolio WHERE quantity != 0 AND execution_type = 'Actual'")
                            
                            for pos in positions:
                                action = "SELL" if pos['quantity'] > 0 else "BUY"
                                qty = abs(pos['quantity'])
                                symbol = pos['symbol']
                                
                                logger.critical(f"Panic Fire: {action} {qty} {symbol}")
                                loop = asyncio.get_running_loop()
                                # Blast market orders rapidly
                                await loop.run_in_executor(None, lambda a=action, s=symbol, q=qty: self.api.place_order(
                                    buy_or_sell=a[0], product_type='I', exchange='NSE', tradingsymbol=s, 
                                    quantity=q, disclosedquantity=0, price_type='MKT', price=0, retention='DAY'
                                ))
                                
                            await conn.execute("UPDATE portfolio SET quantity = 0, avg_price = 0, realized_pnl=0 WHERE execution_type = 'Actual'")
                            
                        logger.critical("✅ Live Market Wipeout Completed.")
                        await send_cloud_alert(
                            "🚨 LIVE PANIC LIQUIDATION COMPLETED\n"
                            "All real positions have been closed with market orders.",
                            alert_type="CRITICAL"
                        )
                        
                except Exception as e:
                    logger.error(f"Live Panic exception: {e}")

async def main():
    logger.info("Initializing Live Bridge...")
    
    mq = MQManager()
    pull_socket = mq.create_subscriber(Ports.ORDERS, topics=[Topics.ORDER_INTENT], bind=True)
    redis_client = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
    
    pool = await asyncpg.create_pool(DB_DSN)
    
    engine = LiveExecutionEngine(mq, pool, redis_client)
    auth_success = await engine.authenticate()
    
    if auth_success:
        try:
            await asyncio.gather(
                engine.run(pull_socket),
                engine.panic_listener()
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
