import json
import logging
import os
from datetime import datetime, timezone
import redis.asyncio as redis

logger = logging.getLogger("CloudAlerts")

class CloudAlerts:
    _instance = None
    _redis = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = CloudAlerts()
        return cls._instance

    def __init__(self):
        redis_host = os.environ.get("REDIS_HOST", "localhost")
        self.redis_url = f"redis://{redis_host}:6379"
        # We don't initialize _redis here because it's async and __init__ is sync.
        # It will be initialized on first alert() call or we can add an init_async method.
        pass

    async def _ensure_redis(self):
        if self._redis is None:
            try:
                self._redis = redis.from_url(self.redis_url, decode_responses=True)
                logger.info(f"CloudAlerts initialized using Redis queue at {self.redis_url}")
            except Exception as e:
                logger.error(f"Failed to initialize Redis for alerts: {e}")

    async def alert(self, text: str, alert_type: str = "SYSTEM", **kwargs):
        """
        Pushes an alert to the local Redis queue.
        Uses redis.asyncio to avoid blocking the event loop.
        """
        await self._ensure_redis()
        if not self._redis:
            logger.warning(f"Redis alerts not initialized. Alert dropped: {text}")
            return

        payload = {
            "message": text, # Match telegram_bot.py expected key
            "type": alert_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs
        }
        
        try:
            # LPUSH is O(1) and atomic
            await self._redis.lpush("telegram_alerts", json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to push alert to Redis: {e}")

# Global helper for easy access
async def send_cloud_alert(text: str, alert_type: str = "SYSTEM", **kwargs):
    await CloudAlerts.get_instance().alert(text, alert_type, **kwargs)
