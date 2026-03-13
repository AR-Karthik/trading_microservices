import base64
import json
import os
import requests
from google.cloud import secretmanager

def get_secret(secret_id, project_id):
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("UTF-8")

def telegram_alert_executor(event, context):
    """Triggered from a message on a Cloud Pub/Sub topic."""
    pubsub_message = base64.b64decode(event['data']).decode('utf-8')
    try:
        data = json.loads(pubsub_message)
    except Exception:
        data = {"text": pubsub_message}

    project_id = os.environ.get("GCP_PROJECT_ID", "karthiks-trading-dashboard")
    
    # Prioritize payload token/chat_id if available, otherwise use Secret Manager
    token = data.get("bot_token") or get_secret("TELEGRAM_BOT_TOKEN", project_id)
    chat_id = data.get("chat_id") or get_secret("TELEGRAM_CHAT_ID", project_id)
    text = data.get("text") or data.get("message") or "Empty Alert Received"

    if not token or not chat_id:
        print("Error: Missing Telegram credentials")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"Alert sent successfully: {text[:50]}...")
    except Exception as e:
        print(f"Failed to send alert: {e}")
