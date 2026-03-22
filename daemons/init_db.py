import asyncio
import asyncpg
import os
import sys
import logging

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.auth import get_db_dsn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InitDB")

async def init_db():
    dsn = get_db_dsn()
    logger.info(f"Connecting to {dsn} for schema initialization...")
    
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        # Local fallback
        if "timescaledb" in dsn:
            dsn_local = dsn.replace("timescaledb", "localhost")
            logger.info(f"Retrying with local address: {dsn_local}...")
            conn = await asyncpg.connect(dsn_local)
        else:
            raise e

    try:
        # 1. Main Trades Table
        logger.info("Initializing 'trades' table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id UUID,
                time TIMESTAMPTZ NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity NUMERIC(15, 4) NOT NULL,
                price NUMERIC(15, 2) NOT NULL,
                fees NUMERIC(10, 2) NOT NULL,
                strategy_id TEXT,
                execution_type TEXT NOT NULL,
                audit_tags JSONB DEFAULT '{}',
                latency_ms NUMERIC(10, 2),
                sequence_id BIGINT,
                PRIMARY KEY (id, time)
            );
        """)
        try:
            await conn.execute("SELECT create_hypertable('trades', 'time', if_not_exists => TRUE);")
        except Exception as e:
            logger.warning(f"Hypertable creation failed for 'trades': {e}")

        # 2. Portfolio State
        logger.info("Initializing 'portfolio' table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                symbol TEXT,
                strategy_id TEXT,
                parent_uuid TEXT,
                underlying TEXT,
                lifecycle_class TEXT DEFAULT 'KINETIC',
                expiry_date DATE,
                quantity NUMERIC(15, 4) NOT NULL DEFAULT 0,
                avg_price NUMERIC(15, 2) DEFAULT 0.0,
                initial_credit NUMERIC(15, 2) DEFAULT 0.0,
                short_strikes JSONB DEFAULT '{}',
                realized_pnl NUMERIC(15, 2) DEFAULT 0.0,
                delta NUMERIC(15, 4) DEFAULT 0.0,
                theta NUMERIC(15, 4) DEFAULT 0.0,
                inception_spot NUMERIC(15, 2) DEFAULT 0.0,
                execution_type TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (symbol, strategy_id, parent_uuid, execution_type)
            );
        """)

        # 3. Counterfactual Ledger (Shadow Trades)
        logger.info("Initializing 'shadow_trades' table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id UUID PRIMARY KEY,
                time TIMESTAMPTZ NOT NULL,
                asset TEXT NOT NULL,
                underlying TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity DOUBLE PRECISION NOT NULL,
                price DOUBLE PRECISION NOT NULL,
                parent_uuid TEXT NOT NULL,
                execution_type TEXT NOT NULL,
                veto_reason TEXT DEFAULT 'NONE',
                is_live BOOLEAN DEFAULT FALSE,
                is_paper BOOLEAN DEFAULT TRUE,
                regime INTEGER,
                s27_quality DOUBLE PRECISION,
                intent_price DOUBLE PRECISION,
                execution_price DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'INTENT'
            );
        """)
        try:
            await conn.execute("SELECT create_hypertable('shadow_trades', 'time', if_not_exists => TRUE);")
        except Exception as e:
            logger.warning(f"Hypertable creation failed for 'shadow_trades': {e}")
            
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_parent ON shadow_trades (parent_uuid);")

        # 4. Rejection Journal
        logger.info("Initializing 'rejections' table...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rejections (
                time TIMESTAMPTZ NOT NULL,
                asset TEXT NOT NULL,
                strategy_id TEXT,
                reason TEXT,
                alpha NUMERIC(15, 4),
                vpin NUMERIC(10, 4),
                PRIMARY KEY (time, asset)
            );
        """)

        logger.info("✅ Database schema synchronized successfully.")
    except Exception as e:
        logger.error(f"❌ Initialization failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(init_db())
