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

class Daemons:
    """Standardized daemon names for health tracking [Audit 11.2]."""
    DATA_GATEWAY = "DataGateway"
    MARKET_SENSOR = "MarketSensor"
    HMM_NIFTY = "HMMEngine_NIFTY50"
    HMM_BANKNIFTY = "HMMEngine_BANKNIFTY"
    HMM_SENSEX = "HMMEngine_SENSEX"
    META_ROUTER = "MetaRouter"
    STRATEGY_ENGINE = "StrategyEngine"
    PAPER_BRIDGE = "PaperBridge"
    LIVE_BRIDGE = "LiveBridge"
    LIQUIDATION_DAEMON = "LiquidationDaemon"
    ORDER_RECONCILER = "OrderReconciler"
    SYSTEM_CONTROLLER = "SystemController"
    DATA_LOGGER = "DataLogger"
    CLOUD_PUBLISHER = "CloudPublisher"
    TELEGRAM_ALERTER = "TelegramAlerter"

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
        # [F7-01] [F11-01] Only include daemons that actually send heartbeats
        self.required_daemons = [
            Daemons.DATA_GATEWAY, 
            Daemons.MARKET_SENSOR, 
            Daemons.HMM_NIFTY,
            Daemons.HMM_BANKNIFTY,
            Daemons.HMM_SENSEX,
            Daemons.META_ROUTER, 
            Daemons.STRATEGY_ENGINE, 
            Daemons.PAPER_BRIDGE, 
            Daemons.LIVE_BRIDGE,
            Daemons.LIQUIDATION_DAEMON, 
            Daemons.ORDER_RECONCILER,
            Daemons.SYSTEM_CONTROLLER,
            Daemons.CLOUD_PUBLISHER,
        ]

    async def get_system_health(self) -> Dict:
        """Computes a health score (0.0 to 1.0) based on daemon heartbeats."""
        now = time.time()
        # Ensure we decode responses if the client doesn't
        heartbeats_raw = await self.redis.hgetall("daemon_heartbeats")
        heartbeats = {}
        for k, v in heartbeats_raw.items():
            key = k if isinstance(k, str) else k.decode()
            val = v if isinstance(v, str) else v.decode()
            heartbeats[key] = val
        
        status = {}
        alive_count = 0
        
        for daemon in self.required_daemons:
            hb_val = heartbeats.get(daemon, 0)
            hb = float(hb_val)
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
