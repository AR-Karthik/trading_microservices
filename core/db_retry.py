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
