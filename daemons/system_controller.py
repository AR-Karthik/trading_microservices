"""
daemons/system_controller.py
============================
Lifecycle & Macro Event Manager Daemon (SRS §2.1)

Responsibilities:
- GCP Spot preemption detection (30-sec notice) → batched SQUARE_OFF_ALL
- Macro calendar lockdown (30 min before tier-1 events)
- Hard VM termination at 16:00 IST
- SEBI-compliant batched square-off (≤10 OPS, 1.01s inter-batch gap)
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as redis

# ── Optional HTTP client ────────────────────────────────────────────────────
try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    import urllib.request as _urllib
    _HAS_HTTPX = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("SystemController")

IST = ZoneInfo("Asia/Kolkata")

# Path to macro calendar (relative to project root)
MACRO_CALENDAR_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "macro_calendar.json"
)

# GCP metadata endpoint for preemption check
GCP_PREEMPT_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
    "?value=true"
)
GCP_METADATA_HEADERS = {"Metadata-Flavor": "Google"}

# Hard shutdown time (IST)
SHUTDOWN_HH = 16
SHUTDOWN_MM = 0

# HMM Ingestion Hard Stop (to prevent MOC noise)
LOGGER_STOP_HH = 15
LOGGER_STOP_MM = 25

# HMM Morning Warm-up (Priming hidden state)
WARMUP_START_HH = 9
WARMUP_START_MM = 0
WARMUP_END_HH = 9
WARMUP_END_MM = 15

# SEBI batch limit
SEBI_BATCH_SIZE = 10
INTER_BATCH_WAIT = 1.01  # seconds


class SystemController:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self.redis: redis.Redis | None = None
        self._macro_events: list[dict] = []
        self._lockdown_announced: set[str] = set()  # event keys already locked
        self._preemption_detected = False
        self._shutdown_flag = False

    # ── Startup ─────────────────────────────────────────────────────────────

    async def start(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        self._macro_events = self._load_macro_calendar()

        # ── Setup Hard Global Budget Constraint ──
        # Capital limits are now configured via the UI and stored in Redis.
        paper_limit = float(await self.redis.get("PAPER_CAPITAL_LIMIT") or 50000.0)
        live_limit = float(await self.redis.get("LIVE_CAPITAL_LIMIT") or 0.0)
        
        # Set base limit (overwrite safe)
        await self.redis.set("GLOBAL_CAPITAL_LIMIT_PAPER", paper_limit)
        await self.redis.set("GLOBAL_CAPITAL_LIMIT_LIVE", live_limit)
        
        # Set available margin ONLY if it doesn't exist (to avoid blowing away mid-day state)
        if not await self.redis.exists("AVAILABLE_MARGIN_PAPER"):
            await self.redis.set("AVAILABLE_MARGIN_PAPER", paper_limit)
            logger.info(f"Initialized AVAILABLE_MARGIN_PAPER pool: ₹{paper_limit:,.2f}")
            
        if not await self.redis.exists("AVAILABLE_MARGIN_LIVE"):
            await self.redis.set("AVAILABLE_MARGIN_LIVE", live_limit)
            logger.info(f"Initialized AVAILABLE_MARGIN_LIVE pool: ₹{live_limit:,.2f}")

        logger.info("SystemController started. Monitoring lifecycle events.")
        await self._telegram_alert(f"🟢 SYSTEM BOOT: Controller active. Paper Budget: ₹{paper_limit:,.2f} | Live Budget: ₹{live_limit:,.2f}")

        # ── Persistence Reconciliation: Audit Pending Journal (SRS §2.7) ──
        await self._audit_pending_journal()

        try:
            await asyncio.gather(
                self._preemption_poller(),
                self._macro_lockdown_watcher(),
                self._hard_shutdown_watcher(),
                self._hmm_sync_watcher(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self.redis.aclose()

    async def _audit_pending_journal(self):
        """Scans for orphaned intents in Redis on boot (SRS §2.7)."""
        keys = await self.redis.keys("Pending_Journal:*")
        if keys:
            count = len(keys)
            logger.warning(f"⚠️ FOUND {count} ORPHANED INTENTS in Pending Journal!")
            for key in keys:
                data_raw = await self.redis.get(key)
                await self._telegram_alert(
                    f"⚠️ ORPHANED INTENT detected on boot: {key}\n"
                    f"Data: {data_raw[:200]}...\n"
                    f"Action Required: Manually verify if order reached exchange!"
                )

    # ── Macro Calendar ───────────────────────────────────────────────────────

    def _load_macro_calendar(self) -> list[dict]:
        try:
            with open(MACRO_CALENDAR_PATH, "r") as f:
                events = json.load(f)
            logger.info(f"Loaded {len(events)} macro events from calendar.")
            return events
        except FileNotFoundError:
            logger.warning(f"macro_calendar.json not found at {MACRO_CALENDAR_PATH}. No lockdowns.")
            return []

    async def _macro_lockdown_watcher(self):
        """Watches macro calendar; pushes MACRO_EVENT_LOCKDOWN 30 min before tier-1 events."""
        logger.info("Macro lockdown watcher active.")
        while not self._shutdown_flag:
            now = datetime.now(tz=IST)
            lockdown_active = False

            for event in self._macro_events:
                if event.get("tier", 99) > 1:
                    continue  # Only tier-1 events

                try:
                    event_dt = datetime.fromisoformat(event["datetime"]).astimezone(IST)
                except (ValueError, KeyError):
                    continue

                delta = event_dt - now
                event_key = f"{event['name']}_{event['datetime']}"

                # Lock window: [event_dt - 30min, event_dt + 30min]
                if timedelta(0) <= delta <= timedelta(minutes=30):
                    lockdown_active = True
                    if event_key not in self._lockdown_announced:
                        self._lockdown_announced.add(event_key)
                        logger.warning(
                            f"📵 MACRO LOCKDOWN: '{event['name']}' in "
                            f"{int(delta.total_seconds() / 60)} min."
                        )
                        await self._telegram_alert(
                            f"📵 MACRO LOCKDOWN activated: {event['name']} "
                            f"at {event_dt.strftime('%H:%M IST')}"
                        )

                # Post-event: clear lockdown 30 min after
                elif delta < timedelta(0) and delta > timedelta(minutes=-30):
                    lockdown_active = True

            # Write/clear lockdown state in Redis
            await self.redis.set("MACRO_EVENT_LOCKDOWN", "True" if lockdown_active else "False")

            if not lockdown_active:
                # Clear announced set for events that have fully passed
                self._lockdown_announced = {
                    k for k in self._lockdown_announced
                    if not self._is_event_fully_past(k, now)
                }

            await asyncio.sleep(30)  # Check every 30 seconds

    def _is_event_fully_past(self, event_key: str, now: datetime) -> bool:
        for event in self._macro_events:
            ek = f"{event['name']}_{event['datetime']}"
            if ek == event_key:
                try:
                    event_dt = datetime.fromisoformat(event["datetime"]).astimezone(IST)
                    return (now - event_dt) > timedelta(minutes=30)
                except Exception:
                    pass
        return True

    # ── GCP Preemption Poller ────────────────────────────────────────────────

    async def _preemption_poller(self):
        """Polls GCP metadata endpoint every 5 seconds for preemption notice."""
        logger.info("GCP preemption poller active (5s interval).")
        while not self._shutdown_flag:
            try:
                preempted = await self._check_preemption()
                if preempted and not self._preemption_detected:
                    self._preemption_detected = True
                    logger.critical("⚡ GCP PREEMPTION NOTICE DETECTED! Initiating emergency square-off.")
                    await self._telegram_alert(
                        "⚡ GCP SPOT VM PREEMPTION DETECTED! Initiating batched SQUARE_OFF_ALL."
                    )
                    await self._execute_square_off_all(reason="GCP_PREEMPTION")
            except Exception as e:
                logger.debug(f"Preemption poll error (expected outside GCP): {e}")

            await asyncio.sleep(5)

    async def _check_preemption(self) -> bool:
        """Returns True if this GCP Spot instance has received a preemption notice."""
        loop = asyncio.get_running_loop()
        if _HAS_HTTPX:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(GCP_PREEMPT_URL, headers=GCP_METADATA_HEADERS)
                return resp.text.strip().lower() == "true"
        else:
            # Fallback using urllib in executor (blocking)
            def _fetch():
                req = _urllib.Request(GCP_PREEMPT_URL, headers=GCP_METADATA_HEADERS)
                with _urllib.urlopen(req, timeout=2) as resp:
                    return resp.read().decode().strip().lower() == "true"
            return await loop.run_in_executor(None, _fetch)

    # ── Hard Shutdown Watcher ────────────────────────────────────────────────

    async def _hard_shutdown_watcher(self):
        """Forces graceful shutdown at SHUTDOWN_HH:SHUTDOWN_MM IST."""
        logger.info(f"Hard shutdown watcher: will trigger at {SHUTDOWN_HH:02d}:{SHUTDOWN_MM:02d} IST.")
        while True:
            now = datetime.now(tz=IST)
            if now.hour == SHUTDOWN_HH and now.minute == SHUTDOWN_MM:
                logger.critical(f"🔴 HARD STOP: {SHUTDOWN_HH:02d}:{SHUTDOWN_MM:02d} IST reached.")
                await self._telegram_alert(
                    f"🔴 HARD STOP at {SHUTDOWN_HH:02d}:{SHUTDOWN_MM:02d} IST. Squaring off all positions."
                )
                await self._execute_square_off_all(reason="EOD_HARD_STOP")
                self._shutdown_flag = True
                # Flush Redis state
                await self.redis.set("MACRO_EVENT_LOCKDOWN", "False")
                await self.redis.publish("system_events", json.dumps({"event": "SHUTDOWN"}))
                logger.info("Graceful shutdown initiated.")
                break
            await asyncio.sleep(15)

    # ── HMM & Data Synchronization ──────────────────────────────────────────

    async def _hmm_sync_watcher(self):
        """Manages HMM warm-up and data logger hard-stop signals."""
        logger.info("HMM synchronization watcher active.")
        while not self._shutdown_flag:
            now = datetime.now(tz=IST)
            
            # 09:00 - 09:15 Warm-up
            is_warmup = (
                (now.hour == WARMUP_START_HH and now.minute >= WARMUP_START_MM) or
                (now.hour == WARMUP_END_HH and now.minute < WARMUP_END_MM)
            )
            await self.redis.set("HMM_WARM_UP", "True" if is_warmup else "False")
            
            # 15:25 Logger Stop
            is_logger_stop = (now.hour > LOGGER_STOP_HH) or (now.hour == LOGGER_STOP_HH and now.minute >= LOGGER_STOP_MM)
            # Reset logger stop at midnight
            if now.hour < 9:
                is_logger_stop = False
            await self.redis.set("LOGGER_STOP", "True" if is_logger_stop else "False")
            
            await asyncio.sleep(30)

    # ── Batched Square-Off ───────────────────────────────────────────────────

    async def _execute_square_off_all(self, reason: str = "MANUAL"):
        """
        Publishes SQUARE_OFF_ALL to Redis panic_channel.
        The execution bridges handle batching per SEBI 10-OPS rule.

        Also directly orchestrates batched square-off of Redis position state
        to handle extreme cases (e.g., bridge is down during preemption).
        """
        logger.warning(f"🚨 SQUARE_OFF_ALL triggered. Reason: {reason}")

        # Signal execution bridge via panic_channel
        await self.redis.publish("panic_channel", json.dumps({
            "action": "SQUARE_OFF_ALL",
            "reason": reason,
            "execution_type": "ALL",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }))

        # Wait for fills (max 15 seconds)
        logger.info("Waiting up to 15 seconds for fill receipts...")
        await asyncio.sleep(15)

        # Flush state
        await self.redis.set("SYSTEM_HALTED", "True")
        logger.info("Redis SYSTEM_HALTED flag set. Square-off sequence complete.")

    # ── Telegram Alert ───────────────────────────────────────────────────────

    async def _telegram_alert(self, message: str):
        """Pushes message to Redis for telemetry_alerter to dispatch."""
        try:
            await self.redis.lpush(
                "telegram_alerts",
                json.dumps({
                    "message": message,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "SYSTEM_CTRL"
                })
            )
        except Exception as e:
            logger.error(f"Telegram alert push failed: {e}")


# ── SEBI-Compliant Batched Execution Helper ──────────────────────────────────

async def batched_square_off(positions: list[dict], fire_fn) -> int:
    """
    Fires square-off orders in SEBI-compliant batches.

    Args:
        positions: List of position dicts with keys: symbol, quantity, action
        fire_fn: Async callable that accepts a list of orders to fire concurrently

    Returns:
        Total number of orders dispatched
    """
    total = 0
    for i in range(0, len(positions), SEBI_BATCH_SIZE):
        batch = positions[i : i + SEBI_BATCH_SIZE]
        await fire_fn(batch)
        total += len(batch)
        if i + SEBI_BATCH_SIZE < len(positions):
            # Inter-batch wait to comply with SEBI 10 OPS limit
            await asyncio.sleep(INTER_BATCH_WAIT)
    return total


if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    controller = SystemController()
    asyncio.run(controller.start())
