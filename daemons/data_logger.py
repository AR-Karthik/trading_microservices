"""
daemons/data_logger.py
======================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Responsibilities:
- Subscribes to Ports.MARKET_STATE (engineered features).
- Batches writes to 'market_history' in TimescaleDB.
- Respects SYSTEM_HALTED and LOGGER_STOP flags.
- Ensures clean data ingestion (all indices: NIFTY, BANKNIFTY, SENSEX).
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
import asyncpg
import redis.asyncio as redis
from core.db_retry import with_db_retry
from core.health import HeartbeatProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("DataLogger")

db_host = os.getenv("DB_HOST", "localhost")
db_user = os.getenv("DB_USER")
db_pass = os.getenv("DB_PASS")
db_name = os.getenv("DB_NAME", "trading_db")

if not db_user or not db_pass:
    logger.error("DB_USER or DB_PASS not set in environment!")
    sys.exit(1)

DB_DSN = f"postgres://{db_user}:{db_pass}@{db_host}:5432/{db_name}"

class DataLogger:
    def __init__(self):
        self.mq = MQManager()
        self.batch = []
        self.batch_lock = asyncio.Lock()
        
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
        self.pool = None

    async def start(self):
        logger.info("Initializing DataLogger...")
        try:
            self.pool = await asyncpg.create_pool(DB_DSN)
        except Exception as e:
            logger.error(f"Failed to connect to TimescaleDB: {e}")
            return

        sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=[Topics.MARKET_STATE])
        
        # Phase 11: Heartbeat
        self.hb = HeartbeatProvider("DataLogger", self._redis)
        asyncio.create_task(self.hb.run_heartbeat())
        
        logger.info("DataLogger active. Listening for market state updates...")
        
        try:
            while True:
                topic, state = await self.mq.recv_json(sub)
                if not state:
                    continue
                
                # Check for logger stop flag (Market on Close prevention)
                logger_stop = await self._redis.get("LOGGER_STOP") == "True"
                if logger_stop:
                    continue

                async with self.batch_lock:
                    self.batch.append(state)
                    
        except asyncio.CancelledError:
            pass
        finally:
            writer_task.cancel()
            await self.pool.close()
            await self._redis.aclose()
            logger.info("DataLogger shut down.")

    async def _reconnect_pool(self):
        """Helper for @with_db_retry to restore connectivity."""
        logger.warning("Attempting to reconnect TimescaleDB pool...")
        if self.pool:
            try:
                await self.pool.close()
            except Exception:
                pass
        self.pool = await asyncpg.create_pool(DB_DSN)

    @with_db_retry(max_retries=5, backoff=2.0)
    async def _batch_writer(self):
        """Periodically flushes the batch to TimescaleDB."""
        while True:
            await asyncio.sleep(10)
            
            async with self.batch_lock:
                if not self.batch:
                    continue
                current_batch = self.batch
                self.batch = []
            
            try:
                async with self.pool.acquire() as conn:
                    # Prepare the data for bulk insert
                    # Columns: time, symbol, price, log_ofi_zscore, cvd, vpin, basis_zscore, vol_term_ratio
                    data_to_insert = []
                    for s in current_batch:
                        # Log all indices and heavyweight signals
                        symbol = s.get("symbol")
                        if not symbol:
                            continue
                            
                        data_to_insert.append((
                            datetime.fromisoformat(s["timestamp"]),
                            s["symbol"],
                            float(s.get("price", 0.0)),
                            float(s.get("log_ofi_zscore", 0.0)),
                            float(s.get("cvd", 0.0)),
                            float(s.get("vpin", 0.0)),
                            float(s.get("basis_zscore", 0.0)),
                            float(s.get("vol_term_ratio", 1.0))
                        ))
                    
                    if data_to_insert:
                        await conn.executemany("""
                            INSERT INTO market_history (time, symbol, price, log_ofi_zscore, cvd, vpin, basis_zscore, vol_term_ratio)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (time, symbol) DO NOTHING
                        """, data_to_insert)
                        logger.info(f"FLUSHED {len(data_to_insert)} records to market_history.")
            except Exception as e:
                logger.error(f"Failed to flush batch: {e}")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    logger_daemon = DataLogger()
    asyncio.run(logger_daemon.start())
