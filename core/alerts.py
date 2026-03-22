"""
Centralized Asynchronous Alerting System
Provides a non-blocking gateway for system-wide notifications using Redis.
"""
import json

import logging
import os
from datetime import datetime, timezone
import redis.asyncio as redis

logger = logging.getLogger("CloudAlerts")

class CloudAlerts:
    # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
    def __init__(self, redis_client):
        self._redis = redis_client

    async def alert(self, text: str, alert_type: str = "SYSTEM", **kwargs):
        """
        Pushes an alert to the Redis queue asynchronously to avoid event loop stalls.
        """
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
async def send_cloud_alert(text: str, alert_type: str = "SYSTEM", redis_client=None, **kwargs):
    if redis_client is None:
        try:
            from core.auth import get_redis_url
            import redis.asyncio as redis
            redis_client = redis.from_url(get_redis_url(), decode_responses=True)
            logger.warning("send_cloud_alert created an unmanaged Redis client. Provide a client for production safety.")
        except ImportError:
            pass
            
    if redis_client:
        alerts = CloudAlerts(redis_client)
        await alerts.alert(text, alert_type, **kwargs)
