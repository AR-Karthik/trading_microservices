import os
import httpx
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("TelegramBot")

class TelegramNotifier:
    """Sends real-time alerts to Telegram."""
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"

    async def send_message(self, text):
        if not self.token or not self.chat_id or "YOUR_BOT_TOKEN_HERE" in self.token:
            logger.warning("Telegram Bot not configured. Skipping alert.")
            return

        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "Markdown"
                }
                response = await client.post(self.api_url, json=payload)
                if response.status_code != 200:
                    logger.error(f"Failed to send Telegram alert: {response.text}")
        except Exception as e:
            logger.error(f"Telegram alerting error: {e}")

async def main():
    # Simple test
    notifier = TelegramNotifier()
    await notifier.send_message("🚀 *Trading System Booted* on GCP standard VM.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
