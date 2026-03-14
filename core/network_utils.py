import asyncio
import logging
import time
from functools import wraps

logger = logging.getLogger("NetworkUtils")

def exponential_backoff(max_retries=5, base_delay=1, max_delay=30):
    """
    Decorator for exponential backoff on async functions.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    retries += 1
                    if retries >= max_retries:
                        logger.error(f"Max retries reached for {func.__name__}: {e}")
                        raise
                    delay = min(base_delay * (2 ** (retries - 1)), max_delay)
                    logger.warning(f"Retry {retries}/{max_retries} for {func.__name__} in {delay}s due to: {e}")
                    await asyncio.sleep(delay)
        return wrapper
    return decorator

class CircuitBreaker:
    """
    Basic Circuit Breaker pattern.
    States: CLOSED (normal), OPEN (tripped), HALF_OPEN (probing)
    """
    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "CLOSED"

    def call(self, func, *args, **kwargs):
        if self.state == "OPEN":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("Circuit Breaker: Entering HALF_OPEN state.")
            else:
                raise Exception("Circuit Breaker is OPEN. Request blocked.")

        try:
            result = func(*args, **kwargs)
            if self.state == "HALF_OPEN":
                logger.info("Circuit Breaker: Success in HALF_OPEN. Resetting to CLOSED.")
                self.reset()
            return result
        except Exception as e:
            self.record_failure()
            raise e

    def reset(self):
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.error(f"Circuit Breaker TRIPPED after {self.failure_count} failures.")
