import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
import asyncpg
from core.mq import MQManager, Ports

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("PaperBridge")

DB_DSN = "postgres://trading_user:trading_pass@localhost:5432/trading_db"

async def init_db(pool):
    """Initializes the TimescaleDB hypertable for ultra-fast historical P&L logging."""
    async with pool.acquire() as conn:
        logger.info("Initializing TimescaleDB schema...")
        
        # 1. Trade Events Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id UUID PRIMARY KEY,
                time TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price NUMERIC(15, 2) NOT NULL,
                fees NUMERIC(10, 2) NOT NULL,
                strategy_id TEXT
            );
        """)
        
        # Make trades a hypertable (TimescaleDB extension feature)
        # Avoid running if already a hypertable
        try:
            await conn.execute("SELECT create_hypertable('trades', 'time');")
            logger.info("trades hypertable created.")
        except asyncpg.exceptions.ObjectNotInPrerequisiteStateError:
            pass # Already a hypertable
        except asyncpg.exceptions.UndefinedFunctionError:
            logger.warning("TimescaleDB extension not active; 'trades' is a normal table.")
            
        # 2. Portfolio State View (Simple table for holding current positions)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT PRIMARY KEY,
                quantity INTEGER NOT NULL DEFAULT 0,
                avg_price NUMERIC(15, 2) DEFAULT 0.0,
                realized_pnl NUMERIC(15, 2) DEFAULT 0.0,
                updated_at TIMESTAMPTZ NOT NULL
            );
        """)
        logger.info("Database schema initialized.")

async def calculate_slippage_and_fees(order):
    """Simulates real-world execution costs."""
    # Base broker fee (e.g., 20 INR per order)
    fees = 20.0
    
    # Random slippage relative to volatility
    slippage_bps = random.uniform(0.5, 2.0) # 0.5 to 2 basis points
    slippage_amount = order['price'] * (slippage_bps / 10000)
    
    # Adjust execution price
    if order['action'] == "BUY":
        execution_price = order['price'] + slippage_amount
    else:
        execution_price = order['price'] - slippage_amount
        
    return execution_price, fees

async def update_portfolio(conn, execution, fees):
    """Maintains the running portfolio state based on executions."""
    symbol = execution['symbol']
    qty = execution['quantity'] if execution['action'] == 'BUY' else -execution['quantity']
    price = execution['price']
    
    # Fetch current state
    record = await conn.fetchrow("SELECT * FROM portfolio WHERE symbol = $1 FOR UPDATE", symbol)
    
    if not record:
        # First trade for this symbol
        await conn.execute(
            """
            INSERT INTO portfolio (symbol, quantity, avg_price, updated_at)
            VALUES ($1, $2, $3, $4)
            """,
            symbol, qty, price, execution['time']
        )
        return
        
    current_qty = record['quantity']
    avg_price = record['avg_price']
    realized_pnl = record['realized_pnl']
    
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
        WHERE symbol = $5
        """,
        new_qty, new_avg_price, realized_pnl, execution['time'], symbol
    )


async def execute_orders(pull_socket, pool, mq_manager):
    """
    Consumes intent signals, simulates execution layer constraints,
    logs results to TimescaleDB for Dashboard/Review.
    """
    logger.info("Starting Paper Bridge execution loop...")
    
    # Optional Pub socket to broadcast trade confirmations
    trade_pub_socket = mq_manager.create_publisher(Ports.TRADE_EVENTS)
    
    while True:
        try:
            # Consume from Strategy Engine via ZeroMQ PUSH/PULL
            topic, order = await mq_manager.recv_json(pull_socket)
            if not order:
                continue
                
            logger.info(f"Received Order: {order['action']} {order['quantity']} {order['symbol']}")
            
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
                "strategy_id": order['strategy_id']
            }
            
            # Persist to Data Vault via asyncpg connection pool
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Log to hypertable
                    await conn.execute(
                        """
                        INSERT INTO trades (id, time, symbol, action, quantity, price, fees, strategy_id)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        """,
                        execution['id'], execution['time'], execution['symbol'], execution['action'],
                        execution['quantity'], execution['price'], execution['fees'], execution['strategy_id']
                    )
                    
                    # 2. Update portfolio summary table
                    await update_portfolio(conn, execution, fees)
            
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
            logger.error(f"Error in execution loop: {e}")
            await asyncio.sleep(1)
            
    trade_pub_socket.close()

async def start_bridge():
    """Initializes Paper Bridge service."""
    logger.info("Initializing Paper Bridge...")
    
    # Ensure event loop compatibility for ZeroMQ
    if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Initialize ZeroMQ Messaging
    mq = MQManager()
    
    # Receive orders from Strategy Engines
    pull_socket = mq.create_pull(Ports.ORDERS)
    
    # Initialize TimescaleDB connection pool
    try:
        pool = await asyncpg.create_pool(DB_DSN)
        await init_db(pool)
    except Exception as e:
        logger.error(f"Failed to connect to TimescaleDB: {e}")
        logger.error("Please ensure the database container is running (docker-compose up -d)")
        return

    try:
        await execute_orders(pull_socket, pool, mq)
    except KeyboardInterrupt:
        logger.info("Shutting down Paper Bridge.")
    finally:
        pull_socket.close()
        mq.context.term()
        await pool.close()

if __name__ == "__main__":
    try:
        if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(start_bridge())
    except KeyboardInterrupt:
        pass
