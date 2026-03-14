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
import asyncpg
import json
import time
import os
import sys
from collections import deque
from redis import asyncio as redis
from core.logger import setup_logger
from core.mq import MQManager, Ports, Topics, NumpyEncoder
from core.alerts import send_cloud_alert
from core.db_retry import with_db_retry

try:
    import uvloop
except ImportError:
    uvloop = None

logger = setup_logger("PaperBridge", log_file="logs/paper_bridge.log")

db_host = os.getenv("DB_HOST", "localhost")
DB_DSN = f"postgres://trading_user:trading_pass@{db_host}:5432/trading_db"

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
        except Exception:
            pass
            
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
                realized_pnl NUMERIC(15, 2) DEFAULT 0.0,
                execution_type TEXT NOT NULL DEFAULT 'Paper',
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (symbol, strategy_id, execution_type, parent_uuid)
            );
        """)

class PaperBridge:
    def __init__(self, mq, pool, redis_client):
        self.mq = mq
        self.pool = pool
        self.redis = redis_client
        self.trade_pub_socket = self.mq.create_publisher(Ports.TRADE_EVENTS)
        self.order_timestamps = deque()
        self.total_realized_pnl = 0.0

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
                INSERT INTO portfolio (symbol, strategy_id, parent_uuid, quantity, avg_price, execution_type, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                symbol, strategy_id, parent_uuid, qty, price, exec_type, execution['time']
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
            SET quantity = $1, avg_price = $2, realized_pnl = $3, updated_at = $4
            WHERE symbol = $5 AND strategy_id = $6 AND execution_type = $7 AND parent_uuid = $8
            """,
            new_qty, new_avg_price, realized_pnl, execution['time'], symbol, strategy_id, exec_type, parent_uuid
        )
        # Update cache for dashboard
        self.total_realized_pnl += (realized_pnl - float(record['realized_pnl']))
        await self.redis.set("DAILY_REALIZED_PNL_PAPER", str(self.total_realized_pnl))

    async def execute_orders(self, pull_socket):
        logger.info("Paper Bridge execution loop active.")
        while True:
            try:
                topic, order = await self.mq.recv_json(pull_socket)
                if not order: continue
                
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
                if len(self.order_timestamps) >= 10:
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
                    "parent_uuid": order.get('parent_uuid', 'NONE')
                }
                
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            "INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
                            execution['id'], execution['time'], execution['symbol'], execution['action'],
                            execution['quantity'], execution['price'], execution['fees'], execution['strategy_id'], execution['execution_type']
                        )
                        await self.update_portfolio(conn, execution, fees)
                
                # Phase 10 Handoff
                await self.redis.publish("NEW_POSITION_ALERTS", json.dumps({
                    "symbol": execution['symbol'],
                    "parent_uuid": execution['parent_uuid'],
                    "strategy_id": execution['strategy_id']
                }))
                
                # ZMQ broadcast
                await self.mq.send_json(self.trade_pub_socket, {"type": "EXECUTION", "symbol": execution['symbol'], "price": execution['price']}, topic=f"EXEC.{execution['symbol']}")
                logger.info(f"Executed {execution['action']} {execution['symbol']} @ {execution['price']}")

            except Exception as e:
                logger.error(f"Execution error: {e}")
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
                
                # Simulate market execution for rollback
                execution = {
                    "id": str(uuid.uuid4()),
                    "time": datetime.now(timezone.utc),
                    "symbol": sym,
                    "action": action,
                    "quantity": abs_qty,
                    "price": 100.0, # Dummy price for rollback simulation
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

async def calculate_slippage_and_fees(order):
    price = float(order['price'])
    slip = random.uniform(0.3, 0.5)
    exec_price = price + slip if order['action'] == 'BUY' else price - slip
    return round(exec_price, 2), 20.0

async def _run_heartbeat(r):
    from core.health import HeartbeatProvider
    hb = HeartbeatProvider("PaperBridge", r)
    await hb.run_heartbeat()

async def main():
    mq = MQManager()
    # Subscribe to ORDER_INTENT and bind so strategies can connect
    pull_socket = mq.create_subscriber(Ports.ORDERS, topics=[Topics.ORDER_INTENT], bind=True)
    r = redis.from_url(f"redis://{os.getenv('REDIS_HOST', 'localhost')}:6379", decode_responses=True)
    pool = await asyncpg.create_pool(DB_DSN)
    await init_db(pool)
    
    bridge = PaperBridge(mq, pool, r)
    await asyncio.gather(bridge.execute_orders(pull_socket), _run_heartbeat(r))

if __name__ == "__main__":
    if uvloop: uvloop.install()
    asyncio.run(main())
