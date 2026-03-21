import asyncpg
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def run():
    host = os.getenv("DB_HOST", "localhost")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASS", "postgres")
    database = os.getenv("DB_NAME", "trading_db")
    
    try:
        # Try localhost if 'postgres' host (docker name) fails on Windows
        try:
            conn = await asyncpg.connect(user=user, password=password, database=database, host=host)
        except:
            conn = await asyncpg.connect(user=user, password=password, database=database, host="localhost")
        print(f"Connected to {database}")
        
        await conn.execute("ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS veto_reason TEXT DEFAULT 'NONE'")
        await conn.execute("ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS is_live BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE shadow_trades ADD COLUMN IF NOT EXISTS is_paper BOOLEAN DEFAULT TRUE")
        
        print("Schema update for Layer 9 complete.")
        await conn.close()
    except Exception as e:
        print(f"Schema update failed: {e}")

if __name__ == "__main__":
    asyncio.run(run())
