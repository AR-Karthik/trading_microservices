"""
core/health.py
==============
Standardized heartbeat and health tracking for Project K.A.R.T.H.I.K.
"""

import time
import json
import logging
import asyncio
from typing import Dict, Optional

logger = logging.getLogger("Health")

class HeartbeatProvider:
    """Mixin or helper to provide heartbeats to Redis."""
    def __init__(self, name: str, redis_client):
        self.name = name
        self.redis = redis_client
        self._stopped = False

    async def run_heartbeat(self, interval: int = 5):
        """Sends a periodic heartbeat to Redis: health:daemon_name = timestamp"""
        logger.info(f"Health heartbeat started for {self.name}")
        while not self._stopped:
            try:
                # Update daemon-specific heartbeat
                ts = time.time()
                await self.redis.hset("daemon_heartbeats", self.name, ts)
                # Keep individual key for easy TTL monitoring if needed
                await self.redis.set(f"heartbeat:{self.name}", ts, ex=30)
            except Exception as e:
                logger.error(f"Heartbeat failed for {self.name}: {e}")
            await asyncio.sleep(interval)

    def stop_heartbeat(self):
        self._stopped = True

class HealthAggregator:
    """Used by SystemController to compute aggregate health scores."""
    def __init__(self, redis_client):
        self.redis = redis_client
        self.required_daemons = [
            "DataGateway", "MarketSensor", "MetaRouter", 
            "StrategyEngine", "PaperBridge", "LiveBridge",
            "LiquidationDaemon", "OrderReconciler"
        ]

    async def get_system_health(self) -> Dict:
        """Computes a health score (0.0 to 1.0) based on daemon heartbeats."""
        now = time.time()
        heartbeats = await self.redis.hgetall("daemon_heartbeats")
        
        status = {}
        alive_count = 0
        
        for daemon in self.required_daemons:
            hb = float(heartbeats.get(daemon, 0))
            is_alive = (now - hb) < 15 # 15s timeout
            status[daemon] = "ALIVE" if is_alive else "DEAD"
            if is_alive:
                alive_count += 1
                
        score = alive_count / len(self.required_daemons) if self.required_daemons else 1.0
        
        return {
            "score": round(score, 2),
            "daemon_status": status,
            "timestamp": now
        }
