import json
import logging
import os
from datetime import datetime, timezone
import redis

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
        try:
            # Note: Using synchronous redis client for simple, thread-safe push
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            logger.info(f"CloudAlerts initialized using Redis queue at {self.redis_url}")
        except Exception as e:
            logger.error(f"Failed to initialize Redis for alerts: {e}")

    async def alert(self, text: str, alert_type: str = "SYSTEM", **kwargs):
        """
        Pushes an alert to the local Redis queue.
        This call is extremely fast (<1ms) and safe for trading loops.
        """
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
            self._redis.lpush("telegram_alerts", json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to push alert to Redis: {e}")

# Global helper for easy access
async def send_cloud_alert(text: str, alert_type: str = "SYSTEM", **kwargs):
    await CloudAlerts.get_instance().alert(text, alert_type, **kwargs)
