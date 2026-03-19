"""
daemons/paper_bridge.py
======================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Responsibilities:
- Simulated order matching and execution (Paper Trading).
- Shadow portfolio ledger management in TimescaleDB.
- Trade event broadcasting for risk and dashboard updates.
"""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
import asyncpg # type: ignore
import json
import time
import os
import zmq
import zmq.asyncio
import sys
import collections
from collections import deque
import redis.asyncio as redis # type: ignore

from core.logger import setup_logger # type: ignore
from core.mq import MQManager, Ports, Topics, NumpyEncoder # type: ignore
from core.alerts import send_cloud_alert # type: ignore
from core.db_retry import robust_db_connect, with_db_retry # type: ignore
from core.health import HeartbeatProvider # type: ignore

try:
    import uvloop # type: ignore
except ImportError:
    uvloop = None

logger = setup_logger("PaperBridge", log_file="logs/paper_bridge.log")

from core.auth import get_db_dsn
DB_DSN = get_db_dsn()

async def init_db(pool):
    """Initializes the TimescaleDB schema."""
    async with pool.acquire() as conn:
        logger.info("Initializing TimescaleDB schema...")
        
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id UUID,
                time TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price NUMERIC(15, 2) NOT NULL,
                fees NUMERIC(10, 2) NOT NULL,
                strategy_id TEXT,
                execution_type TEXT NOT NULL DEFAULT 'Paper',
                audit_tags JSONB DEFAULT '{}',
                PRIMARY KEY (id, time)
            );
        """)
        
        try:
            await conn.execute("SELECT create_hypertable('trades', 'time', if_not_exists => TRUE);")
            logger.info("trades hypertable created.")
        except Exception as e:
            logger.error(f"❌ Failed to create trades hypertable: {e}")
            
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_time ON trades (symbol, time DESC);")
            logger.info("trades index created.")
        except Exception as e:
            logger.error(f"❌ Failed to create trades index: {e}")
            
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT,
                strategy_id TEXT,
                parent_uuid TEXT,
                underlying TEXT,
                lifecycle_class TEXT DEFAULT 'KINETIC',
                expiry_date DATE,
                has_calendar_risk BOOLEAN DEFAULT FALSE,
                quantity INTEGER NOT NULL DEFAULT 0,
                avg_price NUMERIC(15, 2) DEFAULT 0.0,
                initial_credit NUMERIC(15, 2) DEFAULT 0.0,
                short_strikes JSONB DEFAULT '{}',
                realized_pnl NUMERIC(15, 2) DEFAULT 0.0,
                delta NUMERIC(15, 4) DEFAULT 0.0,
                theta NUMERIC(15, 4) DEFAULT 0.0,
                execution_type TEXT NOT NULL DEFAULT 'Paper',
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (symbol, strategy_id, parent_uuid, execution_type)  -- [F6-01]
            );
        """)

        # [D-44] Ensure delta and theta columns exist for existing tables
        try:
            await conn.execute("ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS delta NUMERIC(15, 4) DEFAULT 0.0;")
            await conn.execute("ALTER TABLE portfolio ADD COLUMN IF NOT EXISTS theta NUMERIC(15, 4) DEFAULT 0.0;")
            logger.info("portfolio schema updated with delta/theta columns.")
        except Exception as e:
            logger.warning(f"Portfolio ALTER failed (already exists?): {e}")
        
        try:
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_strategy ON portfolio (strategy_id);")
            logger.info("portfolio index created.")
        except Exception as e:
            logger.error(f"❌ Failed to create portfolio index: {e}")

class PaperBridge:
    def __init__(self, mq, pool, redis_client):
        self.mq = mq
        self.pool = pool
        self.redis = redis_client
        self.trade_pub_socket = self.mq.create_publisher(Ports.TRADE_EVENTS)
        self.order_timestamps = deque()
        self.total_realized_pnl: dict[str, float] = collections.defaultdict(float)

    async def _reconnect_pool(self):
        """Attempt to reconnect the DB pool on failure."""
        try:
            if self.pool:
                await self.pool.close()
            self.pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=5)
            logger.info("✅ DB Pool reconnected successfully.")
        except Exception as e:
            logger.error(f"❌ Failed to reconnect DB pool: {e}")
            raise

    @with_db_retry(max_retries=3, backoff=0.5)
    async def update_portfolio(self, conn, execution, fees):
        """Updates the portfolio balance in the shadow ledger."""
        symbol = execution['symbol']
        strategy_id = execution['strategy_id']
        qty = float(execution['quantity']) if execution['action'] == 'BUY' else -float(execution['quantity'])
        price = float(execution['price'])
        exec_type = execution.get('execution_type', 'Paper')
        parent_uuid = execution.get('parent_uuid', 'NONE')
        
        # 1. Fetch current state
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
                    delta, theta, execution_type, updated_at, has_calendar_risk
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                """,
                symbol, strategy_id, parent_uuid, 
                execution.get('underlying'), 
                execution.get('lifecycle_class', 'KINETIC'),
                execution.get('expiry_date'),
                qty, price, 
                float(execution.get('initial_credit', 0.0)),
                json.dumps(execution.get('short_strikes', {})),
                float(execution.get('delta', 0.0)),
                float(execution.get('theta', 0.0)),
                exec_type, execution['time'],
                execution.get('has_calendar_risk', False)
            )
            return

        current_qty = float(record['quantity'])
        avg_price = float(record['avg_price'])
        realized_pnl = float(record['realized_pnl'])
        
        new_qty = current_qty + qty
        new_avg_price = avg_price
        
        if current_qty > 0 and qty < 0:
            closed_qty = min(current_qty, abs(qty))
            realized_pnl += float(closed_qty) * (float(price) - float(avg_price)) - float(fees)
            if abs(new_qty) < 0.000001: new_avg_price = 0.0
        elif current_qty < 0 and qty > 0:
            closed_qty = min(abs(current_qty), qty)
            realized_pnl += float(closed_qty) * (float(avg_price) - float(price)) - float(fees)
            if abs(new_qty) < 0.000001: new_avg_price = 0.0
        elif abs(new_qty) > abs(current_qty):
            new_avg_price = (abs(current_qty) * avg_price + abs(qty) * price) / abs(new_qty)
            
        await conn.execute(
            """
            UPDATE portfolio
            SET quantity = $1, avg_price = $2, realized_pnl = $3, delta = $4, theta = $5, updated_at = $6
            WHERE symbol = $7 AND strategy_id = $8 AND execution_type = $9 AND parent_uuid = $10
            """,
            new_qty, new_avg_price, realized_pnl, 
            float(execution.get('delta', 0.0)),
            float(execution.get('theta', 0.0)),
            execution['time'], symbol, strategy_id, exec_type, parent_uuid
        )
        # Update PnL cache per asset [Audit Fix]
        underlying = execution.get('underlying') or symbol.split()[0]
        # Standardize underlying
        if "NIFTY" in underlying and "BANK" not in underlying: underlying = "NIFTY50"
        elif "BANK" in underlying: underlying = "BANKNIFTY"
        
        diff = (realized_pnl - float(record['realized_pnl']))
        self.total_realized_pnl[underlying] += diff
        await self.redis.set(f"DAILY_REALIZED_PNL_PAPER:{underlying}", str(self.total_realized_pnl[underlying]))
        
        # Keep global for legacy consumers if needed (additive rule)
        # self.total_realized_pnl_global += diff
        # await self.redis.set("DAILY_REALIZED_PNL_PAPER", str(self.total_realized_pnl_global))

    async def execute_orders(self, pull_socket):
        logger.info("Paper Bridge execution loop active.")
        while True:
            try:
                topic, order = await self.mq.recv_json(pull_socket)
                if not order: continue

                # [Audit 9.1] Strict Paper Filtering: Drop intents not intended for Paper environment
                if order.get("execution_type") != "Paper" and order.get("cmd") not in ["BASKET_ROLLBACK", "FORCE_MARKET_ORDER"]:
                    continue
                
                # Command handling (Phase 13.3)
                if order.get("cmd") == "BASKET_ROLLBACK":
                    await self._handle_basket_rollback(order.get("parent_uuid"))
                    continue
                    
                if order.get("cmd") == "FORCE_MARKET_ORDER":
                    # Re-map command to standard order format for execution
                    order["order_type"] = "MARKET"
                    order["action"] = order.get("side", order.get("action"))
                    order["quantity"] = order.get("qty", order.get("quantity"))
                    # Fall through to execution logic
                
                # Rate limiting
                now = time.time()
                while self.order_timestamps and self.order_timestamps[0] <= now - 1.0:
                    self.order_timestamps.popleft()
                if len(self.order_timestamps) >= 8:  # [F5-02] 10→8 to match spec (8 req/sec)
                    await asyncio.sleep(0.1)
                    continue
                self.order_timestamps.append(now)

                exec_price, fees = await calculate_slippage_and_fees(order)
                execution = {
                    "id": str(uuid.uuid4()),
                    "time": datetime.now(timezone.utc),
                    "symbol": order['symbol'],
                    "action": order['action'],
                    "quantity": order['quantity'],
                    "price": exec_price,
                    "fees": fees,
                    "strategy_id": order['strategy_id'],
                    "execution_type": order.get('execution_type', 'Paper'),
                    "parent_uuid": order.get('parent_uuid', 'NONE'),
                    "delta": order.get('delta', 0.0),
                    "theta": order.get('theta', 0.0)
                }
                
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        # [F4-01] Include audit_tags with heuristic state at time of trade
                        regime_raw = await self.redis.get("hmm_regime") or "UNKNOWN"
                        audit_tags = json.dumps({"regime": regime_raw, "execution_type": execution.get('execution_type', 'Paper')})
                        await conn.execute(
                            "INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type, audit_tags) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                            execution['id'], execution['time'], execution['symbol'], execution['action'],
                            execution['quantity'], execution['price'], execution['fees'], execution['strategy_id'], execution['execution_type'], audit_tags
                        )
                        await self.update_portfolio(conn, execution, fees)
                
                # Phase 10 Handoff
                await self.redis.publish("NEW_POSITION_ALERTS", json.dumps({
                    "symbol": execution['symbol'],
                    "parent_uuid": execution['parent_uuid'],
                    "strategy_id": execution['strategy_id']
                }))
                
                # ZMQ broadcast
                # [F2-01] Fixed: send_json signature is (socket, topic, data) — topic must be 2nd arg
                await self.mq.send_json(self.trade_pub_socket, f"EXEC.{execution['symbol']}", {"type": "EXECUTION", "symbol": execution['symbol'], "price": execution['price']})
                logger.info(f"Executed {execution['action']} {execution['symbol']} @ {execution['price']}")

            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Paper Bridge loop error: {e}")
                await asyncio.sleep(1)

    async def _handle_basket_rollback(self, parent_uuid: str):
        """Phase 13.3: Close all positions associated with a parent_uuid (Circuit Breaker)."""
        if not parent_uuid or parent_uuid == "NONE": return
        
        logger.warning(f"🚨 ROLLBACK triggered for basket {parent_uuid}. Liquidation started.")
        async with self.pool.acquire() as conn:
            # 1. Find all legs currently in portfolio for this basket
            rows = await conn.fetch(
                "SELECT symbol, quantity, strategy_id, execution_type FROM portfolio WHERE parent_uuid = $1 AND quantity != 0",
                parent_uuid
            )
            
            for row in rows:
                sym = row["symbol"]
                qty = float(row["quantity"])
                # To close, we need opposite action
                action = "SELL" if qty > 0 else "BUY"
                abs_qty = abs(qty)
                
                logger.info(f"Rollback: Closing {abs_qty} {sym} to net zero.")
                
                # [Audit 5.2] Fetch actual market price from Redis instead of dummy 100.0
                market_price = 100.0 # fallback
                try:
                    state_raw = await self.redis.get(f"latest_market_state:{sym}")
                    if state_raw:
                        state = json.loads(state_raw)
                        market_price = state.get("price", 100.0) # Will use last traded price
                except Exception as e:
                    logger.warning(f"Rollback could not fetch market price for {sym}: {e}")

                # Simulate market execution for rollback
                execution = {
                    "id": str(uuid.uuid4()),
                    "time": datetime.now(timezone.utc),
                    "symbol": sym,
                    "action": action,
                    "quantity": abs_qty,
                    "price": market_price, # [Audit 5.2] Actual price
                    "fees": 20.0,
                    "strategy_id": row["strategy_id"],
                    "execution_type": row["execution_type"],
                    "parent_uuid": parent_uuid
                }
                
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                        execution['id'], execution['time'], execution['symbol'], execution['action'],
                        execution['quantity'], execution['price'], execution['fees'], execution['strategy_id'], execution['execution_type']
                    )
                    await self.update_portfolio(conn, execution, 20.0)
        
        logger.info(f"✅ Rollback complete for {parent_uuid}. Portfolio flushed.")

    async def _handle_row_liquidation(self, conn, row):
        """Internal helper to close a single portfolio row."""
        sym = row["symbol"]
        qty = float(row["quantity"])
        action = "SELL" if qty > 0 else "BUY"
        abs_qty = abs(qty)
        
        market_price = 100.0 # fallback
        try:
            state_raw = await self.redis.get(f"latest_market_state:{sym}")
            if state_raw:
                state = json.loads(state_raw)
                market_price = state.get("price", 100.0)
        except Exception: pass

        execution = {
            "id": str(uuid.uuid4()),
            "time": datetime.now(timezone.utc),
            "symbol": sym,
            "action": action,
            "quantity": abs_qty,
            "price": market_price,
            "fees": 20.0,
            "strategy_id": row["strategy_id"],
            "execution_type": row.get("execution_type", "Paper"),
            "parent_uuid": row["parent_uuid"]
        }
        
        # Insert trade and update portfolio
        await conn.execute(
            "INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            execution['id'], execution['time'], execution['symbol'], execution['action'],
            execution['quantity'], execution['price'], execution['fees'], execution['strategy_id'], execution['execution_type']
        )
        await self.update_portfolio(conn, execution, 20.0)
        logger.info(f"Targeted Liq: {action} {abs_qty} {sym} @ {market_price}")

    async def panic_listener(self):
        """Phase 13.3: Listens for Global Square Off commands via Redis PubSub."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("panic_channel")
        logger.info("Paper Panic listener active on 'panic_channel'. Listening for Paper mode blasts...")
        
        async for message in pubsub.listen():
            if message['type'] == 'message':
                try:
                    data = json.loads(message['data'])
                    action = data.get('action', '')
                    exec_type = data.get('execution_type', '')
                    if action in ["SQUARE_OFF", "SQUARE_OFF_ALL"] and exec_type in ["Paper", "ALL"]:
                        logger.critical("🚨 RECEIVED PAPER PANIC SIGNAL: SQUARING OFF PAPER POSITIONS! 🚨")
                        
                        async with self.pool.acquire() as conn:
                            rows = await conn.fetch("SELECT parent_uuid FROM portfolio WHERE quantity != 0 AND execution_type = 'Paper'")
                            parent_uuids = set(row["parent_uuid"] for row in rows)
                            
                            for puuid in parent_uuids:
                                await self._handle_basket_rollback(puuid)
                    elif action == "SQUARE_OFF_SIDE" and exec_type in ["Paper", "ALL", ""]:
                        symbol = data.get('symbol')
                        side = data.get('side') # "S" or "B"
                        logger.critical(f"🚨 TRAP ALERT: Selective Paper Liquidation for {symbol} side {side}")
                        
                        async with self.pool.acquire() as conn:
                            rows = await conn.fetch(
                                "SELECT symbol, quantity, strategy_id, parent_uuid FROM portfolio WHERE symbol = $1 AND quantity != 0 AND execution_type = 'Paper'",
                                symbol
                            )
                            for row in rows:
                                if (side == "S" and row['quantity'] > 0) or (side == "B" and row['quantity'] < 0):
                                    # Reuse rollback-style exit logic for this specific row
                                    await self._handle_row_liquidation(conn, row)
                                
                        logger.critical("✅ Paper Panic Square-off Completed.")
                        asyncio.create_task(send_cloud_alert(
                            "🚨 PAPER PANIC LIQUIDATION COMPLETED\n"
                            "All paper positions have been closed with simulated market orders.",
                            alert_type="CRITICAL"
                        ))
                except Exception as e:
                    logger.error(f"Paper Panic exception: {e}")

async def calculate_slippage_and_fees(order: dict) -> tuple[float, float]:
    """Calculates slippage based on index-specific tick size [PB-01]."""
    price = float(order['price'])
    symbol = order['symbol']
    
    # Tick size mapping
    tick_size = 0.05 # Default for NIFTY/BANKNIFTY
    if "SENSEX" in symbol:
        tick_size = 0.05 # SENSEX also 0.05
        
    # [PB-01] Dynamic slippage (2 to 5 ticks)
    slip = random.randint(2, 5) * tick_size
    exec_price = price + slip if order['action'] == 'BUY' else price - slip
    return float(f"{exec_price:.2f}"), 20.0

async def _run_heartbeat(r):
    hb = HeartbeatProvider("PaperBridge", r)
    await hb.run_heartbeat()

async def main():
    mq = MQManager()
    # [Audit 2.2] Pull socket should bind, allowing strategies to connect (push) to it.
    pull_socket = mq.create_pull(Ports.ORDERS, bind=True)
    
    from core.auth import get_redis_url
    redis_url = get_redis_url()
    r = redis.from_url(redis_url, decode_responses=True)
    
    # [Audit 10.1] Connection retry mapping for Database pool creation
    pool = None
    retry_count = 0
    while True:
        try:
            pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=5, timeout=5.0)
            break
        except Exception as e:
            retry_count += 1
            logger.error(f"PaperBridge DB connect failed (Attempt {retry_count}): {e}")
            await asyncio.sleep(min(5 * retry_count, 60))

    if pool:
        await init_db(pool)
        bridge = PaperBridge(mq, pool, r)
        await asyncio.gather(
            bridge.execute_orders(pull_socket),
            bridge.panic_listener(),
            _run_heartbeat(r)
        )
    else:
        logger.critical("Failed to connect to the database after retries. Exiting.")

if __name__ == "__main__":
    if uvloop: uvloop.install()
    asyncio.run(main())
