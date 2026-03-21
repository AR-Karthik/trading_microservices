"""
Database Resilience and Retry Utilities
Implements robust connection patterns and automatic recovery for TimescaleDB.
"""
import asyncio

import functools
import logging
import asyncpg
import os

logger = logging.getLogger(__name__)

def with_db_retry(max_retries=3, backoff=0.5):
    """
    Wraps asynchronous database operations with retry logic, utilizing backoff intervals 
    and attempting automatic pool recovery on persistent connection failures.
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, *args, **kwargs):
            last_err = None
            for attempt in range(max_retries):
                try:
                    return await func(self, *args, **kwargs)
                except (asyncpg.ConnectionDoesNotExistError,
                        asyncpg.InterfaceError, 
                        asyncpg.InternalClientError,
                        OSError) as e:
                    last_err = e
                    logger.warning(f"DB connection error in {func.__name__} (attempt {attempt+1}/{max_retries}): {e}")
                    
                    # Trigger intrinsic pool recovery mechanism if supported by the caller class
                    if hasattr(self, "_reconnect_pool"):
                        try:
                            await self._reconnect_pool()
                        except Exception as re_err:
                            logger.error(f"Failed to reconnect pool: {re_err}")
                    
                    await asyncio.sleep(backoff * (attempt + 1))
            
            logger.critical(f"DB unreachable after {max_retries} retries in {func.__name__}. Last Error: {last_err}")
            # Escalate the terminal exception to ensure critical failures are not silently swallowed
            raise last_err
        return wrapper
    return decorator

async def robust_db_connect(dsn, max_retries=10, timeout=5.0):
    """
    Attempt to create an asyncpg pool with retries and backoff.
    Essential for Docker environments where services might start out of order.
    """
    retry_count = 0
    while True:
        try:
            pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, timeout=timeout)
            logger.info(f"Successfully connected to DB at {dsn}")
            return pool
        except Exception as e:
            retry_count += 1
            if retry_count > max_retries:
                logger.critical(f"Failed to connect to DB after {max_retries} attempts: {e}")
                raise
            wait_time = min(2 * retry_count, 30)
            logger.warning(f"DB Connect Attempt {retry_count} failed: {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
