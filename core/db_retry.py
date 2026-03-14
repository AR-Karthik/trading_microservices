import asyncio
import functools
import logging
import asyncpg
import os

logger = logging.getLogger(__name__)

def with_db_retry(max_retries=3, backoff=0.5):
    """
    Decorator for async methods that use a DB connection pool.
    Retries on connection errors and handles pool reconnection if possible.
    (Phase 11.8: Resilient DB Connection Pool)
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
                    
                    # Try to reconnect pool if the object has a method for it
                    if hasattr(self, "_reconnect_pool"):
                        try:
                            await self._reconnect_pool()
                        except Exception as re_err:
                            logger.error(f"Failed to reconnect pool: {re_err}")
                    
                    await asyncio.sleep(backoff * (attempt + 1))
            
            logger.critical(f"DB unreachable after {max_retries} retries in {func.__name__}. Last Error: {last_err}")
            # Depending on the function, we might want to raise or return a default
            # For most critical logic, raising is safer than silent failure
            raise last_err
        return wrapper
    return decorator
