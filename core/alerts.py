import json
import logging
import os
from google.cloud import pubsub_v1

logger = logging.getLogger("CloudAlerts")

class CloudAlerts:
    _instance = None
    _publisher = None
    _topic_path = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = CloudAlerts()
        return cls._instance

    def __init__(self):
        project_id = os.environ.get("GCP_PROJECT_ID", "karthiks-trading-assistant")
        topic_id = "karthik-telemetry-alerts"
        
        try:
            self._publisher = pubsub_v1.PublisherClient()
            self._topic_path = self._publisher.topic_path(project_id, topic_id)
            logger.info(f"CloudAlerts initialized for topic: {self._topic_path}")
        except Exception as e:
            logger.error(f"Failed to initialize Pub/Sub publisher: {e}")

    async def alert(self, text: str, alert_type: str = "SYSTEM", **kwargs):
        """
        Pushes an alert to Google Cloud Pub/Sub.
        This is a non-blocking/background-compatible call.
        """
        if not self._publisher or not self._topic_path:
            logger.warning(f"Pub/Sub not initialized. Alert dropped: {text}")
            return

        payload = {
            "text": text,
            "type": alert_type,
            "timestamp": os.environ.get("IST_TIME", ""), # Optional time context
            **kwargs
        }
        
        try:
            message_bytes = json.dumps(payload).encode("utf-8")
            # Publish is asynchronous by default in the client
            self._publisher.publish(self._topic_path, message_bytes)
        except Exception as e:
            logger.error(f"Failed to publish alert: {e}")

# Global helper for easy access
async def send_cloud_alert(text: str, alert_type: str = "SYSTEM", **kwargs):
    await CloudAlerts.get_instance().alert(text, alert_type, **kwargs)
