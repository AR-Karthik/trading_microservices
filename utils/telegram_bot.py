"""
utils/telegram_bot.py
=====================
Telemetry Alerter (SRS §5)

Dispatches Telegram alerts for:
- Boot / Shutdown
- Kill Switches
- Macro Lockdowns
- Preemption Notices
- Phantom Orders
- Daily P&L summary

Reads alerts from Redis list "telegram_alerts" and dispatches via Telegram Bot API.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import redis.asyncio as redis

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("TelegramAlerter")

# Alert type → emoji prefix mapping
ALERT_EMOJIS = {
    "SYSTEM_CTRL": "🔧",
    "RECONCILER": "👻",
    "LIQUIDATION": "🚨",
    "CIRCUIT_BREAKER": "🔴",
    "KILL_SWITCH": "🛑",
    "MACRO_LOCKDOWN": "📵",
    "PREEMPTION": "⚡",
    "DAILY_PNL": "📊",
    "BOOT": "🚀",
    "STRATEGY": "📈",
}

# Rate limit: Telegram allows ~30 msgs/sec; we throttle to 1/sec for safety
DISPATCH_INTERVAL_SEC = 1.0


class TelegramAlerter:
    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        redis_url: str = "redis://localhost:6379"
    ):
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.redis_url = redis_url
        self._redis: redis.Redis | None = None
        self._enabled = bool(self.bot_token and self.chat_id)

        if not self._enabled:
            logger.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Alerts will be logged only."
            )

    async def start(self):
        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        logger.info("TelegramAlerter started. Draining redis 'telegram_alerts' queue.")
        await self._send_boot_alert()

        while True:
            try:
                # Block-pop from Redis list (BRPOP = right-pop newest→oldest FIFO)
                result = await self._redis.brpop("telegram_alerts", timeout=30)
                if result:
                    _, raw = result
                    alert = json.loads(raw)
                    await self._dispatch(alert)
                    await asyncio.sleep(DISPATCH_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Alert dispatch error: {e}")
                await asyncio.sleep(5)

        await self._redis.aclose()

    async def _dispatch(self, alert: dict):
        """Sends alert to Telegram (or logs if not configured)."""
        alert_type = alert.get("type", "GENERAL")
        message = alert.get("message", "")
        timestamp = alert.get("timestamp", datetime.now(timezone.utc).isoformat())

        # Fetch live budget context
        total_limit = "N/A"
        avail_margin = "N/A"
        try:
            if self._redis:
                total_limit_val = await self._redis.get("GLOBAL_CAPITAL_LIMIT")
                avail_margin_val = await self._redis.get("AVAILABLE_MARGIN")
                total_limit = f"₹{float(total_limit_val):,.0f}" if total_limit_val else "N/A"
                avail_margin = f"₹{float(avail_margin_val):,.0f}" if avail_margin_val else "N/A"
        except Exception:
            pass

        emoji = ALERT_EMOJIS.get(alert_type, "ℹ️")
        text = f"{emoji} [{alert_type}]\n{message}\n\n💰 Live Budget: {avail_margin} / {total_limit}\n🕐 {timestamp[:19]} UTC"

        logger.info(f"TELEGRAM [{alert_type}]: {message[:100]}")

        if not self._enabled:
            return

        if not _HAS_HTTPX:
            logger.warning("httpx not installed; cannot dispatch Telegram messages.")
            return

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Telegram HTTP error: {e}")

    async def _send_boot_alert(self):
        """Sends system boot notification."""
        await self._redis.lpush("telegram_alerts", json.dumps({
            "type": "BOOT",
            "message": "🟢 Trading System BOOTED\nAll daemons initialising...",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }))

    # ── Convenience helpers for other daemons ─────────────────────────────────

    @staticmethod
    async def push_alert(
        redis_client: redis.Redis,
        message: str,
        alert_type: str = "GENERAL"
    ):
        """Static helper: push alert to Redis queue from any daemon."""
        await redis_client.lpush("telegram_alerts", json.dumps({
            "type": alert_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    alerter = TelegramAlerter()
    asyncio.run(alerter.start())
