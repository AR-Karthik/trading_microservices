import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
import asyncpg
from core.mq import MQManager, Ports, Topics
from redis import asyncio as redis
import json
import time
from collections import deque
import os

try:
    import uvloop
except ImportError:
    uvloop = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("PaperBridge")

db_host = os.getenv("DB_HOST", "localhost")
DB_DSN = f"postgres://trading_user:trading_pass@{db_host}:5432/trading_db"

async def init_db(pool):
    """Initializes the TimescaleDB hypertable for ultra-fast historical P&L logging."""
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
                PRIMARY KEY (id, time)
            );
        """)
        
        # Make trades a hypertable (TimescaleDB extension feature)
        # Avoid running if already a hypertable
        try:
            await conn.execute("SELECT create_hypertable('trades', 'time', if_not_exists => TRUE);")
            logger.info("trades hypertable created or already exists.")
        except Exception as e:
            logger.warning(f"Could not create hypertable: {e}")
        except asyncpg.exceptions.UndefinedFunctionError:
            logger.warning("TimescaleDB extension not active; 'trades' is a normal table.")
            
        # 2. Portfolio State View (Simple table for holding current positions)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT,
                strategy_id TEXT,
                quantity INTEGER NOT NULL DEFAULT 0,
                avg_price NUMERIC(15, 2) DEFAULT 0.0,
                realized_pnl NUMERIC(15, 2) DEFAULT 0.0,
                execution_type TEXT NOT NULL DEFAULT 'Paper',
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (symbol, strategy_id, execution_type)
            );
        """)
        logger.info("Database schema initialized.")

async def calculate_slippage_and_fees(order):
    """
    Simulates real-world options execution costs.
    SRS §5: Paper bridge enforces randomized 0.3–0.5 point slippage on simulated fills.
    This replaces the previous percentage-spread model which was inappropriate for options.
    """
    import random
    fees = 20.0  # Shoonya flat fee per order leg

    # Fixed 0.3–0.5 pt slippage (options tick size granularity)
    slippage = random.uniform(0.3, 0.5)

    price = float(order['price'])
    if order['action'] == 'BUY':
        execution_price = price + slippage   # Pay up
    else:
        execution_price = price - slippage   # Receive less

    return round(execution_price, 2), fees

async def update_portfolio(conn, execution, fees):
    """Maintains the running portfolio state based on executions."""
    symbol = execution['symbol']
    strategy_id = execution['strategy_id']
    exec_type = execution.get('execution_type', 'Paper')
    qty = execution['quantity'] if execution['action'] == 'BUY' else -execution['quantity']
    price = execution['price']
    
    # Fetch current state
    record = await conn.fetchrow("SELECT * FROM portfolio WHERE symbol = $1 AND strategy_id = $2 AND execution_type = $3 FOR UPDATE", symbol, strategy_id, exec_type)
    
    if not record:
        # First trade for this symbol
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
    
    # Calculate realized P&L and adjust average price
    if current_qty > 0 and qty < 0:
        # Selling a long position
        closed_qty = min(current_qty, abs(qty))
        realized_pnl += float(closed_qty) * (float(price) - float(avg_price)) - float(fees)
        if new_qty == 0:
            new_avg_price = 0.0
    elif current_qty < 0 and qty > 0:
        # Covering a short position
        closed_qty = min(abs(current_qty), qty)
        realized_pnl += float(closed_qty) * (float(avg_price) - float(price)) - float(fees)
        if new_qty == 0:
            new_avg_price = 0.0
    elif current_qty >= 0 and qty > 0:
        # Adding to long
        new_avg_price = (float(current_qty * avg_price) + float(qty * price)) / float(new_qty)
    elif current_qty <= 0 and qty < 0:
        # Adding to short
        new_avg_price = (float(abs(current_qty) * avg_price) + float(abs(qty) * price)) / float(abs(new_qty))
        
    await conn.execute(
        """
        UPDATE portfolio
        SET quantity = $1, avg_price = $2, realized_pnl = $3, updated_at = $4
        WHERE symbol = $5 AND strategy_id = $6 AND execution_type = $7
        """,
        new_qty, new_avg_price, realized_pnl, execution['time'], symbol, strategy_id, exec_type
    )


async def execute_orders(pull_socket, pool, mq_manager, redis_client):
    """
    Consumes intent signals, simulates execution layer constraints,
    logs results to TimescaleDB for Dashboard/Review.
    """
    logger.info("Starting Paper Bridge execution loop...")
    
    # Optional Pub socket to broadcast trade confirmations
    trade_pub_socket = mq_manager.create_publisher(Ports.TRADE_EVENTS)
    order_timestamps = deque()
    
    while True:
        try:
            # Consume from Strategy Engine via ZeroMQ PUSH/PULL
            topic, order = await mq_manager.recv_json(pull_socket)
            if not order:
                continue
                
            # --- Rate Limiting Compliance (10 OPS) ---
            while True:
                now = time.time()
                while order_timestamps and order_timestamps[0] <= now - 1.0:
                    order_timestamps.popleft()
                
                if len(order_timestamps) < 10:
                    order_timestamps.append(now)
                    break
                
                wait_time = order_timestamps[0] + 1.001 - now
                await asyncio.sleep(max(0, wait_time))

            logger.info(f"Received Order: {order['action']} {order['quantity']} {order['symbol']} [Strat: {order['strategy_id']}]")
            
            # --- Double-Tap Execution Guard (SRS §2.6) ---
            lock_key = f"lock:{order['symbol']}"
            if await redis_client.exists(lock_key):
                logger.warning(f"❌ DUPLICATE REJECTED: {order['symbol']} has an active lock. Double-tap prevented.")
                continue
            # Lock expires in 10s if not cleared by reconciler (safety fallback)
            await redis_client.setex(lock_key, 10, "LOCKED")

            # --- Pending Journal Persistence (SRS §2.7) ---
            pending_key = f"Pending_Journal:{order['order_id']}"
            await redis_client.set(pending_key, json.dumps(order))
            
            # 1. RISK CHECK: Fetch global risk settings
            global_risk_raw = await redis_client.get("global_risk_settings")
            stop_day_loss = float('inf')
            if global_risk_raw:
                global_risk = json.loads(global_risk_raw)
                stop_day_loss = abs(float(global_risk.get('stop_day_loss', float('inf'))))

            # Fetch current daily realized PnL
            exec_type = order.get('execution_type', 'Paper')
            async with pool.acquire() as conn:
                daily_pnl_record = await conn.fetchrow("""
                    SELECT SUM(CASE WHEN action='SELL' THEN (price * quantity) - fees ELSE -(price * quantity) - fees END) as realized_pnl
                    FROM trades
                    WHERE time >= CURRENT_DATE AND execution_type = $1
                """, exec_type)
                daily_pnl = float(daily_pnl_record['realized_pnl']) if daily_pnl_record and daily_pnl_record['realized_pnl'] else 0.0

            # Stop Day Loss Check: If loss > stop_day_loss, block only NEW opening trades
            is_opening_trade = False
            async with pool.acquire() as conn:
                record = await conn.fetchrow(
                    "SELECT quantity FROM portfolio WHERE symbol = $1 AND strategy_id = $2 AND execution_type = $3", 
                    order['symbol'], order['strategy_id'], exec_type
                )
                current_qty = record['quantity'] if record else 0
                order_qty = order['quantity'] if order['action'] == 'BUY' else -order['quantity']
                if abs(current_qty + order_qty) > abs(current_qty):
                    is_opening_trade = True

            if daily_pnl < -stop_day_loss and is_opening_trade:
                logger.warning(f"BLOCKING ORDER: Daily Loss (₹{abs(daily_pnl):,.2f}) has breached Stop Loss limit (₹{stop_day_loss:,.2f})")
                continue

            # 2. STRATEGY RISK CHECK: Fetch strategy config for Max Capital
            strat_config_raw = await redis_client.hget("active_strategies", order['strategy_id'])
            max_capital = float('inf')
            if strat_config_raw:
                strat_config = json.loads(strat_config_raw)
                max_capital = strat_config.get('max_capital', float('inf'))
            
            # 3. EXPOSURE CHECK
            order_qty = order['quantity'] if order['action'] == 'BUY' else -order['quantity']
            final_qty = current_qty + order_qty
            
            if abs(final_qty) > abs(current_qty):
                # We are increasing exposure or entering fresh
                final_exposure = abs(final_qty * order['price'])
                if final_exposure > max_capital:
                    logger.warning(f"BLOCKING ORDER: {order['strategy_id']} exposure ({final_exposure}) would exceed Max Capital ({max_capital})")
                    continue

            # Apply slippage & fees
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
                "execution_type": exec_type
            }
            
            # Persist to Data Vault via asyncpg connection pool
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Log to hypertable
                    await conn.execute(
                        """
                        INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id, execution_type)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        execution['id'], execution['time'], execution['symbol'], execution['action'],
                        execution['quantity'], execution['price'], execution['fees'], execution['strategy_id'], execution['execution_type']
                    )
                    
                    # 2. Update portfolio summary table
                    await update_portfolio(conn, execution, fees)
            
            # 3. Clear Pending Journal (DB transaction was successful)
            await redis_client.delete(pending_key)
            # Note: lock:{symbol} is cleared by order_reconciler.py as per spec.
            
            # Broadcast executed trade
            await mq_manager.send_json(trade_pub_socket, {
                "id": execution["id"],
                "symbol": execution["symbol"],
                "price": float(exec_price),
                "type": "EXECUTION"
            }, topic=f"EXEC.{execution['symbol']}")
            
            logger.info(f"Executed: {execution['action']} {execution['symbol']} @ {exec_price:.2f} (Fees: {fees})")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Error in execution loop: {e}")
            await asyncio.sleep(1)
            
    trade_pub_socket.close()

async def start_bridge():
    """Initializes Paper Bridge service."""
    logger.info("Initializing Paper Bridge...")
    
    # Ensure event loop compatibility for ZeroMQ
    if uvloop:
        uvloop.install()
    else:
        try:
            if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) # type: ignore
        except Exception:
            pass

    # Initialize ZeroMQ Messaging
    mq = MQManager()
    
    # Receive orders from Strategy Engines via SUB
    pull_socket = mq.create_subscriber(Ports.ORDERS, topics=[Topics.ORDER_INTENT], bind=True)
    
    # Initialize Redis for config fetching
    redis_host = os.getenv("REDIS_HOST", "localhost")
    r = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)
    
    pool = None # Initialize pool to None
    try:
        pool = await asyncpg.create_pool(DB_DSN)
        await init_db(pool)
    except Exception as e:
        logger.error(f"Failed to connect to TimescaleDB: {e}")
        logger.error("Please ensure the database container is running (docker-compose up -d)")
        await r.close() # Close Redis client if DB connection fails
        return

    try:
        # Run execution loop and panic listener concurrently
        await asyncio.gather(
            execute_orders(pull_socket, pool, mq, r),
            panic_listener(pool, r)
        )
    except KeyboardInterrupt:
        logger.info("Shutting down Paper Bridge.")
    finally:
        pull_socket.close()
        mq.context.term()
        if pool:
            await pool.close()
        await r.close()

async def panic_listener(pool, r):
    """Listens for emergency square-off commands from dashboard."""
    pubsub = r.pubsub()
    await pubsub.subscribe("panic_channel")
    logger.info("Panic listener active on 'panic_channel'...")
    
    async for message in pubsub.listen():
        if message['type'] == 'message':
            try:
                import json
                data = json.loads(message['data'])
                if data.get('action') == "SQUARE_OFF":
                    exec_type = data.get('execution_type')
                    mode_str = f"[{exec_type}] " if exec_type else ""
                    logger.critical(f"PANIC SIGNAL RECEIVED: SQUARING OFF {mode_str}ALL POSITIONS")
                    
                    async with pool.acquire() as conn:
                        query = "SELECT symbol, strategy_id, quantity FROM portfolio WHERE quantity != 0"
                        params = []
                        if exec_type:
                            query += " AND execution_type = $1"
                            params.append(exec_type)
                            
                        positions = await conn.fetch(query, *params)
                        for pos in positions:
                            action = "SELL" if pos['quantity'] > 0 else "BUY"
                            logger.info(f"Panic close: {action} {pos['symbol']} Qty {abs(pos['quantity'])}")
                        
                        update_query = "UPDATE portfolio SET quantity = 0, avg_price = 0"
                        if exec_type:
                            update_query += " WHERE execution_type = $1"
                            await conn.execute(update_query, exec_type)
                        else:
                            await conn.execute(update_query)
                            
                        logger.info(f"{mode_str}Portfolio successfully liquidated.")
            except Exception as e:
                logger.error(f"Error in panic handler: {e}")

if __name__ == "__main__":
    try:
        if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) # type: ignore
        asyncio.run(start_bridge())
    except KeyboardInterrupt:
        pass
