import asyncio
import logging
import time
import requests
import os
import signal
from core.mq import MQManager, Ports

logger = logging.getLogger(__name__)

class SystemHealth:
    """
    --- Recommendation 10 & 11: Heartbeat & Preemption ---
    Monitors system vitals and GCP Spot VM termination notices.
    """
    def __init__(self, mq_manager: MQManager):
        self.mq = mq_manager
        self.heartbeat_interval = 5.0 # seconds
        self.preemption_url = "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
        self.is_running = True

    async def start_heartbeat(self):
        """Publishes a heartbeat every 5 seconds."""
        logger.info("💓 Starting Heartbeat Monitor...")
        while self.is_running:
            payload = {
                "type": "HEARTBEAT",
                "timestamp": time.time(),
                "node": "GATEWAY"
            }
            await self.mq.publish(Ports.TICK_STREAM, "HEARTBEAT", payload)
            await asyncio.sleep(self.heartbeat_interval)

    async def watch_preemption(self):
        """
        Polls the Google Metadata server for termination notice.
        If a notice is found, it triggers an emergency liquidation signal.
        """
        if os.getenv("RUNNING_ON_GCP") != "TRUE":
            logger.info("🛡️ Preemption Watcher skipped (Local environment).")
            return

        logger.info("📡 Starting GCP Preemption Watcher...")
        headers = {"Metadata-Flavor": "Google"}
        
        while self.is_running:
            try:
                # GCP Metadata server returns 'TRUE' if preemption is imminent
                response = requests.get(self.preemption_url, headers=headers, timeout=2)
                if response.text == "TRUE":
                    logger.warning("🚨 GCP PREEMPTION DETECTED! Triggering Emergency Close...")
                    # Publish panic signal
                    await self.mq.publish(Ports.TICK_STREAM, "PANIC", {"reason": "GCP_PREEMPTION"})
                    break
            except Exception:
                pass
            await asyncio.sleep(5)

    def stop(self):
        self.is_running = False
