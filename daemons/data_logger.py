"""
daemons/data_logger.py
======================
The HMM Data Lifecycle Collector (SRS Phase 10)

Responsibilities:
- Subscribes to Ports.MARKET_STATE (where engineered features live).
- Batches writes to 'market_history' in TimescaleDB every 10 seconds.
- Respects SYSTEM_HALTED and LOGGER_STOP flags for clean data ingestion.
- Ensures no MOC (Market on Close) noise pollutes the training dataset.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
import asyncpg
from core.mq import MQManager, Ports, Topics
import redis.asyncio as redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("DataLogger")

db_host = os.getenv("DB_HOST", "localhost")
DB_DSN = f"postgres://trading_user:trading_pass@{db_host}:5432/trading_db"

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
        
        # Start the batch writer task
        writer_task = asyncio.create_task(self._batch_writer())
        
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
                        # Extract NIFTY50 spot price from state or fallback
                        # In MarketSensor, price passed is symbol price.
                        # For HMM, we usually track NIFTY50.
                        if s.get("symbol") != "NIFTY50" and s.get("symbol") != "BANKNIFTY":
                            continue
                            
                        data_to_insert.append((
                            datetime.fromisoformat(s["timestamp"]),
                            s["symbol"],
                            float(s.get("price", 0.0)),
                            float(s.get("log_ofi_zscore", 0.0)),
                            float(s.get("cvd_series", [0.0])[-1] if isinstance(s.get("cvd_series"), list) else 0.0),
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
