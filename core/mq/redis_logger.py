import json
from datetime import datetime

class RedisLogger:
    """Streams logs to Redis for dashboard visibility."""
    
    # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
    def __init__(self, redis_client=None, key="live_logs", max_len=100):
        # We explicitly accept an injected redis_client
        self.redis = redis_client
        self.key = key
        self.max_len = max_len

        if self.redis is None:
            # Safe late-binding fallback for unmanaged environments or scripts
            try:
                from core.auth import get_redis_url
                import redis.asyncio as redis_async
                self.redis = redis_async.from_url(get_redis_url())
            except ImportError:
                pass

    async def log(self, message, level="INFO"):
        if not self.redis:
            return  # Graceful skip if no client is injected

        log_entry = json.dumps({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message
        })
        try:
            await self.redis.lpush(self.key, log_entry)
            await self.redis.ltrim(self.key, 0, self.max_len - 1)
        except Exception:
            pass
