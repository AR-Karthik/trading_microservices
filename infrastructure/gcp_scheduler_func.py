import base64
import os
import functions_framework
from google.cloud import compute_v1
from datetime import date
import holidays

# Config
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
ZONE = "asia-south1-a"
INSTANCE_NAME = "trading-engine-spot"

def is_nse_holiday():
    today = date.today()
    if today.weekday() >= 5:
        return True
    india_holidays = holidays.country_holidays("IN", subdiv="MH", years=today.year)
    return today in india_holidays

@functions_framework.pubsub
def schedule_trading_vm(event, context):
    """
    Triggered by Cloud Scheduler via Pub/Sub.
    Expects a message 'START' or 'STOP'.
    """
    pubsub_message = base64.b64decode(event['data']).decode('utf-8').upper()
    print(f"Received action: {pubsub_message}")

    instance_client = compute_v1.InstancesClient()

    if pubsub_message == "START":
        if is_nse_holiday():
            print(f"Skipping START: Today is an NSE holiday or weekend.")
            return
        
        print(f"Starting instance {INSTANCE_NAME}...")
        try:
            instance_client.start(project=PROJECT_ID, zone=ZONE, instance=INSTANCE_NAME)
            print("Start signal sent.")
        except Exception as e:
            print(f"Start failed: {e}")

    elif pubsub_message == "STOP":
        print(f"Stopping instance {INSTANCE_NAME}...")
        try:
            # We use STOP instead of DELETE to preserve the SSD disk (data persistence)
            instance_client.stop(project=PROJECT_ID, zone=ZONE, instance=INSTANCE_NAME)
            print("Stop signal sent.")
        except Exception as e:
            print(f"Stop failed: {e}")
