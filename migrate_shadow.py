import asyncio
import asyncpg
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from core.auth import get_db_dsn

async def migrate():
    dsn = get_db_dsn()
    # If DB_HOST is 'timescaledb', but we are running locally, we might need localhost
    # However, if this is running on the VM, 'timescaledb' might be correct (if in docker)
    # Let's try to override if it fails
    
    print(f"Connecting to {dsn}...")
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as e:
        print(f"Connection to {dsn} failed: {e}")
        # Try localhost fallback if on dev machine
        if "timescaledb" in dsn:
            dsn_local = dsn.replace("timescaledb", "localhost")
            print(f"Retrying with local address: {dsn_local}...")
            conn = await asyncpg.connect(dsn_local)
        else:
            raise e

    try:
        # 1. Create shadow_trades table
        print("Creating shadow_trades table...")
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
                regime INTEGER,
                s27_quality DOUBLE PRECISION,
                status TEXT NOT NULL DEFAULT 'INTENT'
            );
        """)
        
        # 2. Check if it's already a hypertable
        res = await conn.fetchrow("SELECT count(*) FROM _timescaledb_catalog.hypertable WHERE table_name = 'shadow_trades'")
        if res['count'] == 0:
            print("Creating hypertable for shadow_trades...")
            await conn.execute("SELECT create_hypertable('shadow_trades', 'time', if_not_exists => TRUE);")
        
        # 3. Add index for faster PhD analysis
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_shadow_parent ON shadow_trades (parent_uuid);")
        
        print("✅ Migration completed successfully.")
    except Exception as e:
        print(f"❌ Migration failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(migrate())
