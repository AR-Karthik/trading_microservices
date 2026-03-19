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
        from core.auth import get_redis_url
        self.redis_url = get_redis_url()
        # Redis connection is deferred to _ensure_redis to prevent blocking the synchronous constructor.
        self._redis = None

    async def _ensure_redis(self):
        if self._redis is None:
            try:
                self._redis = redis.from_url(self.redis_url, decode_responses=True)
                logger.info("CloudAlerts initialized with authenticated Redis.")
            except Exception as e:
                logger.error(f"Failed to initialize Redis for alerts: {e}")

    async def alert(self, text: str, alert_type: str = "SYSTEM", **kwargs):
        """
        Pushes an alert to the Redis queue asynchronously to avoid event loop stalls.
        """
        await self._ensure_redis()
        if not self._redis:
            logger.warning(f"Redis alerts not initialized. Alert dropped: {text}")
            return

        payload = {
            "message": text,  # Key required by external telegram consumers
            "type": alert_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs
        }
        
        try:
            # Execute atomic left-push to the alert queue
            await self._redis.lpush("telegram_alerts", json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to push alert to Redis: {e}")

# Global entrypoint for triggering alerts without managing the singleton instance directly
async def send_cloud_alert(text: str, alert_type: str = "SYSTEM", **kwargs):
    await CloudAlerts.get_instance().alert(text, alert_type, **kwargs)
