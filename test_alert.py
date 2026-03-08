import redis
import json
import os
from datetime import datetime, timezone

def test_alert():
    host = os.getenv("REDIS_HOST", "localhost")
    r = redis.Redis(host=host, port=6379, decode_responses=True)
    
    alert = {
        "type": "STRATEGY",
        "message": "✅ TEST ALERT: Trading System Live on GCP. Docker/Tailscale/Async Redis Verified!",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    r.lpush("telegram_alerts", json.dumps(alert))
    print(f"Pushed test alert to {host}:6379")

if __name__ == "__main__":
    test_alert()
