"""
daemons/system_controller.py
============================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Responsibilities:
- GCP Spot preemption detection & batched SQUARE_OFF_ALL.
- Macro calendar lockdown (30 min before tier-1 events).
- Hard VM termination at 16:00 IST.
- SEBI-compliant batched square-off.
- Multi-index history synchronization (NIFTY, BANKNIFTY, SENSEX).
"""

import asyncio
import time
import json
import logging
import os
import zmq
import zmq.asyncio
import sys
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from typing import Dict, List, Tuple, Optional, TYPE_CHECKING
if TYPE_CHECKING:
    from core.health import HealthAggregator, HeartbeatProvider
from core.alerts import send_cloud_alert
from core.mq import MQManager, Ports, NumpyEncoder
from core.margin import AsyncMarginManager

try:
    import asyncpg
    _HAS_ASYNCPG = True
except ImportError:
    _HAS_ASYNCPG = False

try:
    import uvloop
except ImportError:
    uvloop = None

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

# Regime Ingestion Hard Stop (to prevent MOC noise)
LOGGER_STOP_HH = 15
LOGGER_STOP_MM = 25

# Regime Engine parameters (Deterministic)
HISTORY_FETCH_HH = 9
HISTORY_FETCH_MM = 0
LOOKBACK_DAYS = 14

# 3-Stage EOD Timeline
HALT_KINETIC_HH, HALT_KINETIC_MM = 15, 0
SQUARE_OFF_KINETIC_HH, SQUARE_OFF_KINETIC_MM = 15, 20
SHUTDOWN_HH, SHUTDOWN_MM = 16, 0 

# SEBI batch limit
SEBI_BATCH_SIZE = 10
INTER_BATCH_WAIT = 1.01  # seconds

# Margin Reserve
HEDGE_RESERVE_PCT = 0.15

# T-1 Calendar Guard
EXPIRY_MATRIX = {
    "BANKNIFTY": {"expiry_day": 2, "sweep_time": (15, 15)},  # Wednesday -> Tuesday 15:15
    "NIFTY50":   {"expiry_day": 3, "sweep_time": (15, 15)},  # Thursday -> Wednesday 15:15
    "SENSEX":    {"expiry_day": 4, "sweep_time": (15, 15)},  # Friday -> Thursday 15:15
}


class SystemController:
    def __init__(self, redis_url: str = None):
        if redis_url is None:
            from core.auth import get_redis_url
            redis_url = get_redis_url()
        self.redis_url = redis_url
        self.redis: redis.Redis | None = None
        self._macro_events: list[dict] = []
        self._lockdown_announced: set[str] = set()  # event keys already locked
        self._preemption_detected = False
        self._shutdown_flag = False
        self.margin_manager: Optional[AsyncMarginManager] = None
        self.health_agg: Optional["HealthAggregator"] = None
        self.hb: Optional["HeartbeatProvider"] = None
        self._last_live_limit = 0.0
        self._last_paper_limit = 0.0
        self._background_tasks: set[asyncio.Task] = set()
        self._last_cash_live = 0.0
        self._last_coll_live = 0.0
        self._last_cash_paper = 0.0
        self._last_coll_paper = 0.0 # [Audit 22.3] Track capital for dynamic sync
        
        # Shoonya API removed [Audit 14.1: Centralized in SnapshotManager]
        self.api = None
        self.pool: asyncpg.Pool | None = None
        self._boot_time = time.time()

    # ── Startup ─────────────────────────────────────────────────────────────

    async def start(self):
        # [Constitutional Purity] Isolate System Controller to management CPU cores
        self._pin_core()
        
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        # [F10-01] Add DB connection retry loop
        retry_count = 0
        from core.auth import get_db_dsn
        dsn = get_db_dsn()
        while True:
            try:
                self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=20, timeout=30.0, command_timeout=10.0)
                logger.info("✅ SystemController connected to TimescaleDB.")
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"SystemController DB connect failed (Attempt {retry_count}): {e}")
                await asyncio.sleep(min(5 * retry_count, 60))
        self._macro_events = self._load_macro_calendar()
        self.margin_manager = AsyncMarginManager(self.redis)

        # ── Setup Hard Global Budget Constraint ──
        # Capital limits will be managed dynamically via _periodic_margin_sync [Audit 22.4]
        live_limit = float(await self.redis.get("LIVE_CAPITAL_LIMIT") or 0.0)
        paper_limit = float(await self.redis.get("PAPER_CAPITAL_LIMIT") or 50000.0)
        self._last_live_limit = live_limit
        
        eff_live = float(live_limit * (1 - HEDGE_RESERVE_PCT))
        res_live = float(live_limit * HEDGE_RESERVE_PCT)
        
        # Initial Boot Allocation
        cash_live = eff_live * 0.5
        coll_live = eff_live * 0.5
        await self.redis.set("CASH_COMPONENT_LIVE", f"{cash_live:.2f}")
        await self.redis.set("COLLATERAL_COMPONENT_LIVE", f"{coll_live:.2f}")
        await self.redis.set("HEDGE_RESERVE_LIVE", f"{res_live:.2f}")
        await self.redis.set("AVAILABLE_MARGIN_PAPER", f"{paper_limit:.2f}")
        await self.redis.set("AVAILABLE_MARGIN_LIVE", f"{live_limit:.2f}")
        
        # [Audit-Fix] Start periodic margin sync
        asyncio.create_task(self._periodic_margin_sync())

        # Paper Pipeline (Parity for Shadow Graduation)
        eff_paper = float(paper_limit * (1 - HEDGE_RESERVE_PCT))
        res_paper = float(paper_limit * HEDGE_RESERVE_PCT)
        
        cash_paper = eff_paper * 0.5
        coll_paper = eff_paper * 0.5
        
        await self.redis.set("CASH_COMPONENT_PAPER", f"{cash_paper:.2f}")
        await self.redis.set("COLLATERAL_COMPONENT_PAPER", f"{coll_paper:.2f}")
        await self.redis.set("HEDGE_RESERVE_PAPER", f"{res_paper:.2f}")
        
        logger.info(f"⚖️ REGULATORY SPLIT (50:50): Live Cash={cash_live}, Coll={coll_live} | Paper Cash={cash_paper}, Coll={coll_paper}")
        
        await self.redis.set("PAPER_CAPITAL_LIMIT", paper_limit)
        await self.redis.set("LIVE_CAPITAL_LIMIT", live_limit)

        # [Audit-Fix 24.1] Synchronize tracking variables with initial boot allocation
        # Prevents 'Delta-Double' bug on the first periodic sync iteration
        self._last_live_limit = live_limit
        self._last_paper_limit = paper_limit
        self._last_cash_live = cash_live
        self._last_coll_live = coll_live
        self._last_cash_paper = cash_paper
        self._last_coll_paper = coll_paper
        
        # Set available margin pools if they don't exist
        for suffix in ["LIVE", "PAPER"]:
            if not await self.redis.exists(f"CASH_COMPONENT_{suffix}"):
                limit = live_limit if suffix == "LIVE" else paper_limit
                eff = limit * (1 - HEDGE_RESERVE_PCT)
                await self.redis.set(f"CASH_COMPONENT_{suffix}", f"{eff*0.5:.2f}")
                await self.redis.set(f"COLLATERAL_COMPONENT_{suffix}", f"{eff*0.5:.2f}")
                logger.info(f"Initialized {suffix} Cash/Collateral components (50:50 split)")

        logger.info("SystemController started. Monitoring lifecycle events.")
        asyncio.create_task(send_cloud_alert(
            f"🟢 SYSTEM BOOT: Controller active. Paper Budget: ₹{paper_limit:,.2f} | Live Budget: ₹{live_limit:,.2f}",
            alert_type="SYSTEM"
        ))

        # ── Persistence Reconciliation: Audit Pending Journal (SRS §2.7) ──
        await self._audit_pending_journal()

        try:
            # Schedule all long-running tasks
            self._track_task(asyncio.create_task(self._preemption_poller()), "PreemptionPoller")
            self._track_task(asyncio.create_task(self._macro_lockdown_watcher()), "MacroLockdownWatcher")
            self._track_task(asyncio.create_task(self._regime_sync_watcher()), "RegimeSyncWatcher")
            self._track_task(asyncio.create_task(self._three_stage_eod_scheduler()), "EODScheduler")
            self._track_task(asyncio.create_task(self._t1_calendar_sweep_watcher()), "T1CalendarSweep") # Phase 3
            self._track_task(asyncio.create_task(self._unsettled_premium_quarantine()), "PremiumQuarantine") # Phase 3.3
            self._track_task(asyncio.create_task(self._quarterly_settlement_guard()), "QuarterlySettlementGuard")    # Phase 3.4
            self._track_task(asyncio.create_task(self._hard_state_sync()), "HardStateSync")          # Spec 11.6: Ghost Fill Sync
            self._track_task(asyncio.create_task(self._exchange_health_monitor()), "ExchangeHealthMonitor")   # Spec 12.4: NSE Halt Switch
            self._track_task(asyncio.create_task(self._daily_lookback_scheduler()), "DailyLookbackScheduler")
            self._track_task(asyncio.create_task(self._pre_market_validator_scheduler()), "PreMarketValidator")
            
            # ── Phase 9: UI & Observability ──
            from core.health import HealthAggregator, HeartbeatProvider
            self.health_agg = HealthAggregator(self.redis)
            self.hb = HeartbeatProvider("SystemController", self.redis)
            if self.hb:
                self._track_task(asyncio.create_task(self.hb.run_heartbeat()), "Heartbeat")
            self._track_task(asyncio.create_task(self._health_aggregation_loop()), "HealthAggregationLoop")
            # [Phase 14] High-Frequency Watchdog (1000ms threshold)
            self._track_task(asyncio.create_task(self._high_freq_watchdog_loop()), "HighFreqWatchdog")
            # [Phase 14] Nuclear Panic Subscriber
            self._track_task(asyncio.create_task(self._panic_subscriber_loop()), "PanicSubscriber")
            self._track_task(asyncio.create_task(self._run_metrics_api()), "MetricsAPI")

            logger.info("System Controller [Core 3] initialized and monitoring.")
            # Keep the main task alive indefinitely or until shutdown_flag is set
            while not self._shutdown_flag:
                await asyncio.sleep(1) # Sleep briefly to allow other tasks to run

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"FATAL: System Controller failed to start: {e}")
            await send_cloud_alert(f"💀 FATAL: System Controller failed to start: {e}", alert_type="CRITICAL")
            raise
        finally:
            if self.redis:
                await self.redis.aclose()
            if self.pool:
                await self.pool.close()

    def _pin_core(self):
        """
        [Constitutional Purity] Isolate management daemons to separate CPU cores.
        Prevents interference with high-performance mathematical workloads.
        """
        cpu_cores_str = os.getenv("SYSTEM_CONTROLLER_CPU_CORES", "")
        if cpu_cores_str:
            try:
                core_ids = [int(x.strip()) for x in cpu_cores_str.split(",") if x.strip()]
                if core_ids:
                    os.sched_setaffinity(0, set(core_ids))
                    logger.info(f"📌 System Controller pinned to CPU cores: {core_ids}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to pin System Controller to CPU cores {cpu_cores_str}: {e}")
        else:
            # Respect OS/CGroup defaults if not explicitly configured
            pass

    def _track_task(self, task: asyncio.Task, name: str):
        """[Audit-Fix 26.1] Prevents 'Silent Task Death' by keeping references and logging failures."""
        self._background_tasks.add(task)
        def _cleanup(t: asyncio.Task):
            self._background_tasks.discard(t)
            try:
                if not t.cancelled() and t.exception():
                    exc = t.exception()
                    logger.error(f"🚨 CRITICAL TASK FAILURE: {name} died with {exc}")
                    asyncio.create_task(send_cloud_alert("SYSTEM_RECOVERY_FAILURE", f"Task {name} died unexpectedly: {exc}"))
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass
        task.add_done_callback(_cleanup)

    async def _audit_pending_journal(self):
        """Scans for orphaned intents in Redis on boot (SRS §2.7)."""
        keys = await self.redis.keys("Pending_Journal:*")
        if keys:
            count = len(keys)
            logger.warning(f"⚠️ FOUND {count} ORPHANED INTENTS in Pending Journal!")
            for key in keys:
                data_raw = await self.redis.get(key)
                asyncio.create_task(send_cloud_alert(
                    f"⚠️ ORPHANED INTENT detected on boot: {key}\n"
                    f"Data: {data_raw[:200]}...\n"
                    f"Action Required: Manually verify if order reached exchange!",
                    alert_type="CRITICAL"
                ))

    # ── Macro Calendar ───────────────────────────────────────────────────────

    def _load_macro_calendar(self) -> list[dict]:
        try:
            with open(MACRO_CALENDAR_PATH, "r") as f:
                events = json.load(f)
            logger.info(f"Loaded {len(events)} macro events from calendar.")
            return events
        except FileNotFoundError:
            logger.warning(f"macro_calendar.json not found at {MACRO_CALENDAR_PATH}. No lockdowns.")
            # Phase 15: Institutional CAUTIOUS_MODE fallback if blind
            if self.redis:
                asyncio.create_task(self.redis.set("CAUTIOUS_MODE", "True"))
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
                        asyncio.create_task(send_cloud_alert(
                            f"📵 MACRO LOCKDOWN activated: {event['name']} "
                            f"at {event_dt.strftime('%H:%M IST')}",
                            alert_type="SYSTEM"
                        ))

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
                
            # Phase 17: Unbounded Memory Flush
            if now.hour == 8 and now.minute == 0:
                self._lockdown_announced.clear()

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
                    # [Audit 21.2] Instant Halt to prevent new originations during emergency square-off
                    await self.redis.set("SYSTEM_HALT", "True")
                    logger.critical("⚡ GCP PREEMPTION NOTICE DETECTED! Initiating emergency square-off.")
                    asyncio.create_task(send_cloud_alert(
                        "⚡ GCP SPOT VM PREEMPTION DETECTED! Initiating batched SQUARE_OFF_ALL.",
                        alert_type="CRITICAL"
                    ))
                    # [Audit 23.2] Shield emergency sequence from SIGTERM cancellation
                    await asyncio.shield(self._execute_square_off_all(reason="GCP_PREEMPTION"))
            except Exception as e:
                logger.debug(f"Preemption poll error (expected outside GCP): {e}")

            await asyncio.sleep(1) # Accelerated 1s loop for preemption resilience

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

    # ── EOD Daily Summary ────────────────────────────────────────────────────

    async def _eod_summary_report(self, now: datetime):
        """
        Collects end-of-day trading statistics from Redis and TimescaleDB,
        then pushes a structured summary to the Telegram alert queue.
        Called once at 16:00 IST before EOD square-off.
        """
        date_str = now.strftime("%d-%b-%Y")
        logger.info(f"Generating EOD summary report for {date_str}...")

        try:
            # ── Capital & Margin (from Redis) ──────────────────────────────────
            paper_limit    = float(await self.redis.get("PAPER_CAPITAL_LIMIT") or 0)
            live_limit     = float(await self.redis.get("LIVE_CAPITAL_LIMIT") or 0)
            avail_paper    = float(await self.redis.get("AVAILABLE_MARGIN_PAPER") or 0)
            avail_live     = float(await self.redis.get("AVAILABLE_MARGIN_LIVE") or 0)
            margin_used    = float(await self.redis.get("CURRENT_MARGIN_UTILIZED") or 0)

            deployed_paper = paper_limit - avail_paper
            deployed_live  = live_limit  - avail_live

            # ── Realized P&L (from Redis — updated by bridges after each fill) ──
            pnl_paper = float(await self.redis.get("DAILY_REALIZED_PNL_PAPER") or 0)
            pnl_live  = float(await self.redis.get("DAILY_REALIZED_PNL_LIVE")  or 0)

            # ── Active Lots Remaining ─────────────────────────────────────────
            active_lots = int(await self.redis.get("ACTIVE_LOTS_COUNT") or 0)

            # ── Trade Count & Open Positions (from TimescaleDB) ───────────────
            trade_count_paper = 0
            trade_count_live  = 0
            open_positions    = []

            if _HAS_ASYNCPG:
                try:
                    async with self.pool.acquire() as conn:
                        # Trade count for today
                        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                        row = await conn.fetchrow("""
                            SELECT
                                COUNT(*) FILTER (WHERE execution_type = 'PAPER') AS paper_count,
                                COUNT(*) FILTER (WHERE execution_type = 'ACTUAL')  AS live_count
                            FROM trades
                            WHERE time >= $1 AND time <= $2
                        """, today_start, now)
                        if row:
                            trade_count_paper = row["paper_count"] or 0
                            trade_count_live  = row["live_count"]  or 0

                        # Phase 9: DB-Verified Today's P&L (Institutional Integrity)
                        # [Audit 23.5] Carry-Forward Bias Fix: created_at >= $1 ensures we only sum intraday alpha
                        p_row = await conn.fetchrow("SELECT SUM(realized_pnl) as pnl FROM portfolio WHERE updated_at >= $1 AND created_at >= $1 AND execution_type = 'PAPER'", today_start)
                        l_row = await conn.fetchrow("SELECT SUM(realized_pnl) as pnl FROM portfolio WHERE updated_at >= $1 AND created_at >= $1 AND execution_type = 'ACTUAL'", today_start)
                        
                        db_pnl_paper = float(p_row['pnl'] or 0.0)
                        db_pnl_live  = float(l_row['pnl'] or 0.0)
                        
                        # Verification check: If Redis vs DB mismatch > 1.0, flag it
                        if abs(db_pnl_live - pnl_live) > 1.0:
                            logger.warning(f"P&L Desync Detect: Redis={pnl_live}, DB={db_pnl_live}. Using DB as truth.")
                            pnl_live = db_pnl_live
                        if abs(db_pnl_paper - pnl_paper) > 1.0:
                             pnl_paper = db_pnl_paper

                        # Open positions (non-zero quantity)
                        positions = await conn.fetch("""
                            SELECT symbol, strategy_id, execution_type, quantity, avg_price, realized_pnl
                            FROM portfolio
                            WHERE quantity != 0
                            ORDER BY execution_type, symbol
                        """)
                        for p in positions:
                            open_positions.append(
                                f"  {'📄' if p['execution_type'] == 'PAPER' else '🟢'} "
                                f"{p['symbol']} | Qty: {p['quantity']:+d} | "
                                f"Avg: ₹{float(p['avg_price']):,.2f} | "
                                f"Strat: {p['strategy_id']}"
                            )
                except Exception as db_err:
                    logger.error(f"EOD DB query failed: {db_err}")
                    open_positions = ["  ⚠️ DB unavailable — check manually"]

            # ── Format the Summary Message ────────────────────────────────────
            pnl_paper_emoji = "🟢" if pnl_paper >= 0 else "🔴"
            pnl_live_emoji  = "🟢" if pnl_live  >= 0 else "🔴"

            lines = [
                f"📊 *EOD Daily Summary — {date_str}*",
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "💰 *Capital*",
                f"  Paper Limit:    ₹{paper_limit:>10,.0f}",
                f"  Available:      ₹{avail_paper:>10,.0f}",
                f"  Deployed Today: ₹{deployed_paper:>10,.0f}",
                "",
                f"  Live  Limit:    ₹{live_limit:>10,.0f}",
                f"  Available:      ₹{avail_live:>10,.0f}",
                f"  Deployed Today: ₹{deployed_live:>10,.0f}",
                f"  Margin Utilized:₹{margin_used:>10,.0f}",
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "📈 *Realized P&L*",
                f"  {pnl_paper_emoji} Paper: ₹{pnl_paper:>+10,.2f}",
                f"  {pnl_live_emoji}  Live:  ₹{pnl_live:>+10,.2f}",
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━",
                "📋 *Trades Executed Today*",
                f"  📄 Paper: {trade_count_paper} trades",
                f"  🟢 Live:  {trade_count_live} trades",
                f"  🎯 Active Lots Open: {active_lots}",
                "",
            ]

            if open_positions:
                lines += [
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                    "🔓 *Open Positions (will be squared off)*",
                ] + open_positions + [""]
            else:
                lines += [
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                    "✅ *No open positions.*",
                    "",
                ]

            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
            summary_msg = "\n".join(lines)

            asyncio.create_task(send_cloud_alert(summary_msg, alert_type="SUMMARY"))
            logger.info("EOD summary report sent to Telegram.")

        except Exception as e:
            logger.error(f"EOD summary report failed: {e}")
            asyncio.create_task(send_cloud_alert(f"⚠️ EOD summary report failed: {e}", alert_type="ERROR"))

    # ── Regime Data Synchronization ──────────────────────────────────────────

    async def _regime_sync_watcher(self):
        """Manages Regime data logger hard-stop signals."""
        logger.info("Regime synchronization watcher active.")
        while not self._shutdown_flag:
            now = datetime.now(tz=IST)
            
            # 15:25 Logger Stop
            is_logger_stop = (now.hour > LOGGER_STOP_HH) or (now.hour == LOGGER_STOP_HH and now.minute >= LOGGER_STOP_MM)
            # Reset logger stop at midnight
            if now.hour < 9:
                is_logger_stop = False
            await self.redis.set("LOGGER_STOP", "True" if is_logger_stop else "False")
            
            await asyncio.sleep(60)

    # ── 14D History Fetch (Phase 0) ──────────────────────────────────────────

    async def _three_stage_eod_scheduler(self):
        """Phase 2: Orchestrates 15:00, 15:15, 15:20 triggers."""
        logger.info("EOD Lifecycle Scheduler active.")
        while not self._shutdown_flag:
            try:
                now = datetime.now(IST)
                
                # 1. 15:00 IST - HALT_KINETIC (Origination Stop)
                if now.hour == HALT_KINETIC_HH and now.minute == HALT_KINETIC_MM:
                    await self.redis.set("HALT_KINETIC", "True")
                    logger.warning("📵 15:00 IST: KINETIC origination halted. Only exits allowed.")
                    await send_cloud_alert("📵 15:00 IST: KINETIC origination halted. Existing trades running to exit.", alert_type="SYSTEM")
                
                # 2. 15:20 IST - SQUARE_OFF_INTRADAY (Aggressive Intraday Liquidation)
                if now.hour == SQUARE_OFF_KINETIC_HH and now.minute == SQUARE_OFF_KINETIC_MM:
                    logger.warning("🔴 15:20 IST: INTRADAY square-off initiated (KINETIC + ZERO_DTE).")
                    await self._execute_selective_square_off(lifecycle_class="KINETIC", reason="EOD_HARD_INTRADAY")
                    await self._execute_selective_square_off(lifecycle_class="ZERO_DTE", reason="EOD_HARD_INTRADAY")
                    await send_cloud_alert("🔴 15:20 IST: Intraday square-off complete. Institutional positional trades hibernating.", alert_type="RISK")
                
                # 3. 15:45 IST - POST_MARKET_AUDIT
                if now.hour == 15 and now.minute == 45:
                    logger.info("📊 15:45 IST: Triggering Post-Market Performance Audit.")
                    await self._spawn_ephemeral_audit("daemons.post_market_audit")
                    await asyncio.sleep(60) # Prevent multiple triggers

                # 4. 16:00 IST - HARD_VM_SHUTDOWN
                if now.hour == SHUTDOWN_HH and now.minute == SHUTDOWN_MM:
                    logger.critical("💀 16:00 IST: HARD SHUTDOWN sequence started.")
                    # ── EOD Summary Report (before square-off) ──
                    await self._eod_summary_report(now)
                    # POSITIONAL trades hibernate - no square_off_all here
                    logger.critical("💾 POSITIONAL trades hibernated in DB. Server shutting down.")
                    
                    # Phase 11: Flush EOD P&L keys against Kill-Switch persistence loops
                    await self.redis.delete("DAILY_REALIZED_PNL_PAPER")
                    await self.redis.delete("DAILY_REALIZED_PNL_LIVE")
                    await self.redis.delete("SYSTEM_HALT")
                    self._shutdown_flag = True
                
            except Exception as e:
                logger.error(f"EOD Scheduler error: {e}")
            
            await asyncio.sleep(60)

    async def _unsettled_premium_quarantine(self):
        """Phase 3.3: Prevents using option sell premium until T+1 settlement."""
        logger.info("Unsettled premium quarantine active. Monitoring T+1 credit.")
        while not self._shutdown_flag:
            try:
                now = datetime.now(IST)
                # Phase 3.3.1: Release quarantine from yesterday
                if now.hour == 9 and now.minute == 0:
                    await self.redis.set("QUARANTINE_PREMIUM", "0.00")
                    await asyncio.sleep(60) # Avoid repeat
                    continue

                # Find aggregate credit premium from today's orders
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                async with self.pool.acquire() as conn:
                    # [Audit-Fix 26.2] The Paper-Profit Lock: Only quarantine ACTUAL profits.
                    # Prevents Shadow/Paper success from locking real Live Cash.
                    net_row = await conn.fetchrow("""
                        SELECT SUM(realized_pnl) as net_pnl 
                        FROM portfolio 
                        WHERE updated_at >= $1 AND execution_type = 'ACTUAL'
                    """, today_start)
                    if net_row and net_row['net_pnl'] and float(net_row['net_pnl']) > 0:
                        # Phase 38: Correct Net vs Gross Premium Trap (Quarantine Net only)
                        credit = float(net_row['net_pnl'])
                        await self.redis.set("QUARANTINE_PREMIUM", f"{credit:.2f}")
                        logger.debug(f"Quarantined ₹{credit:.2f} in unsettled premium.")
            except Exception as e:
                logger.error(f"Quarantine error: {e}")
            await asyncio.sleep(300)

    async def _quarterly_settlement_guard(self):
        """Phase 3.4: SEBI Quarterly Settlement logic - halts trading if credit is due."""
        logger.info("Quarterly settlement guard active.")
        while not self._shutdown_flag:
            try:
                now = datetime.now(IST)
                # Check if it's the first Friday of the quarter (common settlement day)
                # This is a heuristic approximation for the demo
                is_settlement_day = (now.month in [1, 4, 7, 10] and now.weekday() == 4 and now.day <= 7)
                
                if is_settlement_day and now.hour == 9 and now.minute == 0:
                    logger.warning("🏛️ SEBI QUARTERLY SETTLEMENT: Halting all new entries for credit transfer.")
                    await self.redis.set("SYSTEM_HALT", "True")
                    await send_cloud_alert("🏛️ SEBI Quarterly Settlement detected. Trading halted for manual fund reconciliation.", alert_type="SYSTEM")
                    await asyncio.sleep(60) # Avoid repeat
            except Exception as e:
                logger.error(f"System Controller loop error: {e}")
                await asyncio.sleep(1)
            await asyncio.sleep(3600)

    async def _t1_calendar_sweep_watcher(self):
        """Phase 3: Liquidates T-1 expiries for calendar spreads."""
        logger.info("T-1 Calendar sweep watcher active.")
        while not self._shutdown_flag:
            try:
                now = datetime.now(IST)
                # Sweep at 15:15 on T-1
                if now.hour == 15 and now.minute == 15:
                    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                    async with self.pool.acquire() as conn:
                        # Find POSITIONAL positions expiring tomorrow
                        rows = await conn.fetch("""
                            SELECT DISTINCT parent_uuid, underlying 
                            FROM portfolio 
                            WHERE has_calendar_risk = TRUE 
                              AND lifecycle_class = 'POSITIONAL' 
                              AND expiry_date = (SELECT MIN(expiry_date) FROM portfolio WHERE expiry_date > $1)
                              AND quantity != 0
                        """, today_start)
                        for row in rows:
                            logger.warning(f"⏳ T-1 CALENDAR SWEEP: Liquidating {row['parent_uuid']}")
                            await self.redis.publish("panic_channel", json.dumps({
                                "action": "FORCE_LIQUIDATE_T1_MARGIN",
                                "parent_uuid": row['parent_uuid'],
                                "reason": "T1_CALENDAR_RISK"
                            }))
                            await send_cloud_alert(f"⏳ T-1 Guard: Liquidating {row['underlying']} calendar [{row['parent_uuid']}] to prevent margin spike.", alert_type="RISK")
                    await asyncio.sleep(65)
            except Exception as e:
                logger.error(f"Calendar sweep error: {e}")
            await asyncio.sleep(30)

    async def _execute_selective_square_off(self, lifecycle_class: str, reason: str):
        """Phase 2.4: Targets specific positions in TimescaleDB with SEBI-compliant batching."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT symbol, parent_uuid, quantity, execution_type 
                FROM portfolio 
                WHERE lifecycle_class = $1 AND quantity != 0
            """, lifecycle_class)
            
            if not rows:
                logger.info(f"No {lifecycle_class} positions found to liquidate.")
                return

            logger.warning(f"Liquidating {len(rows)} {lifecycle_class} positions. Reason: {reason}")
            
            # SEBI 10-ops rule: Batch by 10, wait 1.01s
            for i in range(0, len(rows), SEBI_BATCH_SIZE):
                batch = rows[i:i + SEBI_BATCH_SIZE]
                for row in batch:
                    await self.redis.publish("panic_channel", json.dumps({
                        "action": "FORCE_LIQUIDATE",
                        "symbol": row['symbol'],
                        "parent_uuid": row['parent_uuid'],
                        "qty": row['quantity'],
                        "execution_type": row['execution_type'],
                        "reason": reason
                    }))
                logger.info(f"SEBI Batch processed ({len(batch)} orders). Guard interval active...")
                await asyncio.sleep(INTER_BATCH_WAIT)

    async def _daily_lookback_scheduler(self):
        """Schedules and executes the 14-day daily close fetch at 09:00 IST."""
        logger.info("Daily lookback scheduler active.")
        while not self._shutdown_flag:
            now = datetime.now(tz=IST)
            
            # [Audit-Fix] DataGateway now handles this. Just monitor for existence.
            if not await self.redis.exists("history_14d:NIFTY50"):
                if (now.hour == 9 and now.minute > 5) or now.hour > 9:
                    logger.warning("🕒 history_14d missing! DataGateway sync might have failed.")
                await asyncio.sleep(60)
                continue
            
            await asyncio.sleep(30)

    async def _pre_market_validator_scheduler(self):
        """Triggers the pre-market validator at 09:00 IST."""
        logger.info("Pre-market validator scheduler active.")
        while not self._shutdown_flag:
            try:
                now = datetime.now(IST)
                # Ensure we only trigger on weekdays (0-4 are Mon-Fri)
                if now.weekday() < 5 and now.hour == 9 and now.minute == 0:
                    logger.info("🛡️ 09:00 IST: Triggering Pre-Market Validator.")
                    await self._spawn_ephemeral_audit("daemons.pre_market_validator")
                    await asyncio.sleep(60) # Prevent re-trigger within the same minute
            except Exception as e:
                logger.error(f"Pre-Market Scheduler error: {e}")
            await asyncio.sleep(30)

    async def _spawn_ephemeral_audit(self, module_name: str):
        """Spawns read-only audits as ephemeral sub-processes to prevent GIL lock or memory drift."""
        try:
            cmd = [sys.executable, "-m", module_name]
            logger.info(f"Spawning ephemeral process: {' '.join(cmd)}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            async def _monitor_proc():
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    logger.info(f"✅ {module_name} completed successfully.")
                else:
                    logger.error(f"❌ {module_name} failed with code {proc.returncode}:\n{stderr.decode('utf-8', errors='ignore')}")
            
            self._track_task(asyncio.create_task(_monitor_proc()), f"Monitor_{module_name}")
        except Exception as e:
            logger.error(f"Failed to spawn {module_name}: {e}")

    # ── Hard State Sync ──────────────────────────────────────────────────────

    async def _hard_state_sync(self):
        """Every 15 minutes, compares broker truth (from Redis) with DB 'portfolio' table."""
        logger.info("Ghost Fill Sync task started (15m interval).")
        while not self._shutdown_flag:
            try:
                # 1. Fetch broker truth from Redis Hash (populated by SnapshotManager)
                truth_map_raw = await self.redis.hgetall("broker_positions")
                
                # Phase 8: Safe Serialization against bad manager state
                truth_map = {}
                for k, v in truth_map_raw.items():
                    if v:
                        try: truth_map[k] = int(v)
                        except ValueError: pass
                
                # Phase 32: Veto if BROKER_TRUTH is stale
                broker_ts = await self.redis.get("TIMESTAMP:BROKER_TRUTH")
                if not broker_ts or (time.time() - float(broker_ts)) > 60:
                    logger.warning("Ghost Sync Vetoed: SnapshotManager telemetry is stale or missing (>60s).")
                    await asyncio.sleep(5)
                    continue

                # If no truth in Redis, maybe SnapshotManager haven't polled yet. Wait.
                if not truth_map:
                    logger.debug("No broker truth in Redis yet. Skipping sync.")
                    await asyncio.sleep(60)
                    continue
                
                # 2. Fetch DB state
                async with self.pool.acquire() as conn:
                    db_positions = await conn.fetch("SELECT symbol, SUM(quantity) as qty FROM portfolio GROUP BY symbol")
                    db_map = {p['symbol']: int(p['qty']) for p in db_positions if int(p['qty']) != 0}
                    
                    # 3. Compare
                    all_symbols = set(truth_map.keys()) | set(db_map.keys())
                    
                    for sym in all_symbols:
                        t_qty = truth_map.get(sym, 0)
                        d_qty = db_map.get(sym, 0)
                        
                        if t_qty != d_qty:
                            if t_qty > d_qty:
                                amount = t_qty - d_qty
                                # Orphan detected: Broker has more, we don't.
                                logger.critical(f"🚨 ORPHAN POSITION DETECTED: {sym} | Broker: {t_qty}, DB: {d_qty}")
                                await self.redis.publish("panic_channel", json.dumps({
                                    "action": "CLOSE_POSITION",
                                    "symbol": sym,
                                    "qty": abs(amount),
                                    "reason": "GHOST_SYNC_GAP"
                                }))
                                await send_cloud_alert(f"🚨 ORPHAN POSITION: Broker sees {t_qty} of {sym} but DB only {d_qty}. Liquidation triggered.", alert_type="CRITICAL")
                            
                            elif t_qty < d_qty:
                                # Phase 20/23/36: DB ghost detected: Broker has less. Update DB only!
                                logger.warning(f"👻 STALE GHOST DETECTED: {sym} | Cleaning DB state.")
                                await conn.execute("UPDATE portfolio SET quantity = $2 WHERE symbol = $1", sym, t_qty)
                                await send_cloud_alert(f"👻 STALE GHOST: DB showed {d_qty} for {sym} but broker only {t_qty}. DB cleaned.", alert_type="WARNING")

            except Exception as e:
                logger.error(f"Ghost sync error: {e}")
            
            await asyncio.sleep(900) # 15 minutes

    async def _periodic_margin_sync(self):
        """[Audit 22.5] Dynamically syncs capital limits and regulatory split from UI."""
        logger.info("Monitoring Redis for dynamic capital limit updates (60s interval).")
        while not self._shutdown_flag:
            try:
                # 1. Check for UI-driven limit changes
                live_limit_now = float(await self.redis.get("LIVE_CAPITAL_LIMIT") or 0.0)
                
                if live_limit_now != self._last_live_limit:
                    eff_live = live_limit_now * (1 - HEDGE_RESERVE_PCT)
                    res_live = live_limit_now * HEDGE_RESERVE_PCT
                    
                    # Phase 21: Collateral Haircut drift prevention
                    coll_haircut = float(await self.redis.get("CONFIG:COLLATERAL_HAIRCUT") or 0.90)
                    
                    cash_live_new = eff_live * 0.5
                    coll_live_new = (eff_live * 0.5) * coll_haircut
                    
                    delta_cash = cash_live_new - self._last_cash_live
                    delta_coll = coll_live_new - self._last_coll_live
                    
                    # [Audit 23.4] Atomic Delta Adjustment: Prevent overwriting active reservations
                    await self.margin_manager.sync_capital(delta_cash, delta_coll, live_limit_now, execution_type="ACTUAL")
                    await self.redis.set("HEDGE_RESERVE_LIVE", f"{res_live:.2f}")
                    
                    logger.warning(f"🏦 DYNAMIC CAPITAL UPDATE: Limit changed to {live_limit_now}. Atomic Δ-sync applied.")
                    self._last_live_limit = live_limit_now
                    self._last_cash_live = cash_live_new
                    self._last_coll_live = coll_live_new

                # 2. Consumer-only logging for observability
                m_avail = await self.redis.get("ACCOUNT:MARGIN:AVAILABLE")
                m_used = await self.redis.get("ACCOUNT:MARGIN:USED")
                if m_avail:
                    logger.debug(f"📊 Margin Health: Available={m_avail} | Used={m_used}")
            except Exception as e:
                logger.error(f"Margin sync failed: {e}")
            
            await asyncio.sleep(60)

    # ── Exchange Health Monitor (Spec 12.4) ──────────────────────────────────

    async def _exchange_health_monitor(self):
        """Phase 12.2: High-Precision Circuit Breaker (Auto-Halt)."""
        logger.info("Exchange health monitor active. Threshold: 500ms / 3 missing ticks.")
        missing_ticks: int = 0
        
        while not self._shutdown_flag:
            try:
                # Only monitor during active market hours
                now = datetime.now(tz=IST)
                if not (now.hour == 9 and now.minute >= 15) and not (now.hour > 9 and now.hour < 15) and not (now.hour == 15 and now.minute <= 30):
                    await asyncio.sleep(60)
                    continue

                # Check age of all critical indices to prevent single-index blindspot
                any_stale = False
                for index in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    tick_raw = await self.redis.get(f"latest_tick:{index}")
                    if tick_raw:
                        tick = json.loads(tick_raw)
                        tick_ts_str = tick.get("timestamp") # ISO format
                        if tick_ts_str:
                            tick_ts = datetime.fromisoformat(tick_ts_str).astimezone(timezone.utc)
                            now_utc = datetime.now(timezone.utc)
                            latency_ms = (now_utc - tick_ts).total_seconds() * 1000
                            
                            # Phase 12.2: > 500ms jitter or stale feed
                            if latency_ms > 500:
                                any_stale = True
                                logger.warning(f"⚠️ {index} Feed Latency Spike: {latency_ms:.0f}ms")
                                break
                    else:
                        any_stale = True
                        break
                        
                if any_stale:
                    missing_ticks = missing_ticks + 1
                    logger.warning(f"⚠️ FEED STALL: {missing_ticks}/3 missing ticks. Threshold 500ms.")
                else:
                    missing_ticks = 0

                if missing_ticks >= 3:
                    logger.critical(f"🚨 NSE_HALT_SWITCH: Stale feed ({missing_ticks} misses). Suspending all routing.")
                    await self.redis.set("SYSTEM_HALT", "True") # Phase 12.4
                    await self.redis.publish("panic_channel", json.dumps({
                        "action": "SUSPEND_ALL_ROUTING",
                        "reason": "EXCHANGE_FEED_STALL"
                    }))
                    await send_cloud_alert("🚨 EXCHANGE FEED STALL: 3 consecutive misses / >500ms jitter. SYSTEM_HALT active.", alert_type="CRITICAL")
                    
                    # Wait for manual recovery
                    while await self.redis.get("SYSTEM_HALT") == "True":
                        await asyncio.sleep(5)
                    missing_ticks = 0
                    logger.info("🟢 System manually resumed from halt.")

            except Exception as e:
                logger.error(f"Exchange monitor error: {e}")
                
            await asyncio.sleep(0.2) # High-precision 200ms check loop

    # ── Batched Square-Off ───────────────────────────────────────────────────

    async def _execute_square_off_all(self, reason: str = "MANUAL"):
        """
        Publishes SQUARE_OFF_ALL to Redis panic_channel.
        The execution bridges handle batching per SEBI 10-OPS rule.
        """
        # Phase 27: Double-Tap Command Storm Lock
        # [Audit-Fix 24.2] Tighten lock TTL to 10s for emergency retry flexibility
        # 60s is too long for a 30s GCP preemption window.
        if not await self.redis.set("SQUARE_OFF_LOCK", "LOCKED", nx=True, ex=10):
            logger.warning(f"SQUARE_OFF_ALL already in progress. Ignoring {reason}.")
            return
            
        logger.warning(f"🚨 SQUARE_OFF_ALL triggered. Reason: {reason}")

        # Signal execution bridge via panic_channel
        # [Audit 23.3] Subscriber-Zero Check: Ensure at least one bridge received the command
        active_subs = await self.redis.publish("panic_channel", json.dumps({
            "action": "SQUARE_OFF_ALL",
            "reason": reason,
            "execution_type": "ALL",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }))
        if active_subs == 0:
            logger.critical(f"🚨 SUBSCRIBER-ZERO DETECTED: No active bridges received SQUARE_OFF_ALL signal ({reason}).")
            await send_cloud_alert("🚨 EMERGENCY: Panic channel has 0 subscribers. Square-off signal lost!", alert_type="CRITICAL")

        # Positive Acknowledgement Loop (Sprint timeout, max 5s total -> 25 iters * 0.2s)
        logger.info("Awaiting db quantity square-off confirmations (Sprint 5s max)...")
        for _ in range(25):
            try:
                async with self.pool.acquire() as conn:
                    val = await conn.fetchval("SELECT SUM(ABS(quantity)) FROM portfolio WHERE quantity != 0")
                    # [Audit 23.6] Handle potential None from empty portfolio
                    f_val = float(val) if val is not None else 0.0
                    if f_val == 0:
                        logger.info("✅ All positions zeroed successfully.")
                        await send_cloud_alert("✅ FINAL_STATE:FLAT. Square-off structurally verified within preemption window.", alert_type="SYSTEM")
                        break
            except Exception as e:
                logger.error(f"SQL Verification error during square-off loop: {e}")
            await asyncio.sleep(0.2)

        # Flush state
        await self.redis.set("SYSTEM_HALTED", "True")
        logger.info("Redis SYSTEM_HALTED flag set. Square-off sequence complete.")
        
        # Institutional Issue 5: Verified GCP Shutdown
        if reason == "GCP_PREEMPTION":
            logger.critical("💀 Writing FINAL_STATE:FLAT to avoid blocking the OS preemption sequence.")
            await self.redis.set("FINAL_STATE:FLAT", "True")

    # ── Phase 9: Observability Helpers ────────────────────────────────────────

    async def _health_aggregation_loop(self):
        """Periodically computes system health score based on heartbeats."""
        while not self._shutdown_flag:
            try:
                if self.health_agg:
                    health = await self.health_agg.get_system_health()
                    await self.redis.set("SYSTEM_HEALTH_REPORT", json.dumps(health, cls=NumpyEncoder))
                    # Update high-level flag for metrics API
                    await self.redis.set("SYSTEM_HEALTH_SCORE", f"{health['score']:.2f}")
            except Exception as e:
                logger.error(f"Health aggregation failed: {e}")
            await asyncio.sleep(10)

    async def _high_freq_watchdog_loop(self):
        """[Phase 14] Monitors critical daemons with 1000ms threshold."""
        logger.info("🛡️ High-Frequency Watchdog active (1000ms threshold).")
        critical_daemons = ["MetaRouter", "OrderReconciler", "MarketSensor", "StrategyEngine"]
        
        while not self._shutdown_flag:
            try:
                now = time.time()
                for daemon in critical_daemons:
                    hb_raw = await self.redis.get(f"HEARTBEAT:{daemon}")
                    if hb_raw:
                        hb_ts = float(hb_raw)
                        if now - hb_ts > 1.0: # 1000ms breach
                            logger.critical(f"💀 DAEMON DEATH DETECTED: {daemon} (Drift: {now - hb_ts:.2f}s)")
                            await send_cloud_alert(
                                f"💀 DAEMON DEATH DETECTED: {daemon}\n"
                                f"System Health is compromised. Manual intervention or automated restart required.",
                                alert_type="CRITICAL",
                                redis_client=self.redis
                            )
                            # Logic for automated restart could be added here if infra supports it
            except Exception as e:
                logger.error(f"High-Freq Watchdog error: {e}")
            await asyncio.sleep(0.5) # 500ms resolution

    async def _panic_subscriber_loop(self):
        """[Phase 14] Nuclear Kill-Switch Listener."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("panic_channel")
        logger.info("📡 SystemController subscribed to panic_channel for Nuclear Kill-Switch.")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    if data.get("action") == "SQUARE_OFF_ALL":
                        # Double-check reason to avoid loop if triggered from here
                        reason = data.get("reason", "EXTERNAL_PANIC")
                        if "NUCLEAR" in reason: # Special flag for external panic
                            await self._execute_square_off_all(reason=reason)
                except Exception as e:
                    logger.error(f"Panic subscriber error: {e}")

    async def _run_metrics_api(self):
        """Lightweight REST endpoint for dashboard metrics (Spec 9.1)."""
        try:
            from fastapi import FastAPI, HTTPException
            from pydantic import BaseModel
            from typing import Dict
            import uvicorn
            
            app = FastAPI(title="ControllerMetrics")

            class GateConfig(BaseModel):
                gates: Dict[str, str]

            @app.post("/config/gates")
            async def update_gates(config: GateConfig):
                """Phase 9.1: Dynamically updates the MetaRouter risk gates."""
                if config.gates:
                    # Sync to Redis hash for MetaRouter to pick up (Lab Control Step 22.9)
                    await self.redis.hset("CONFIG:GATE_CONTROL", mapping=config.gates)
                    # Signal MetaRouter to refresh veto gates immediately
                    await self.redis.publish("panic_channel", json.dumps({"action": "GATE_SYNC"}))
                    logger.warning(f"🏛️ GATE CONTROL UPDATE: {config.gates}")
                    return {"status": "success", "updated_gates": config.gates}
                raise HTTPException(status_code=400, detail="No gates provided")

            @app.get("/config/gates")
            async def get_gates():
                """Returns the current state of all risk gates."""
                return await self.redis.hgetall("CONFIG:GATE_CONTROL")

            @app.get("/health")
            async def get_health():
                raw = await self.redis.get("SYSTEM_HEALTH_REPORT")
                return json.loads(raw) if raw else {"status": "starting"}

            @app.get("/health/telemetry")
            async def get_telemetry():
                # [Audit-Fix] Enriched Telemetry for Dashboard
                # 1. Fetch heartbeats from LH: keys (Bridges) and daemon_heartbeats hash
                heartbeats = {}
                keys = await self.redis.keys("LH:*")
                for k in keys:
                    v = await self.redis.get(k)
                    if v:
                        name = k.split(":")[1]
                        heartbeats[name] = json.loads(v)
                
                # 2. Add standard daemons from HealthAggregator
                health_report = await self.health_agg.get_system_health()
                for name, status in health_report.get("daemon_status", {}).items():
                    if name not in heartbeats:
                        heartbeats[name] = {
                            "status": status,
                            "timestamp": datetime.fromtimestamp(health_report["timestamp"], tz=timezone.utc).isoformat()
                        }

                # 3. Latency Metrics
                nse_lat = float(await self.redis.get("TELEMETRY:LATENCY:NSE") or 0.45)
                bse_lat = float(await self.redis.get("TELEMETRY:LATENCY:BSE") or 0.82)
                
                return {
                    "live_heartbeats": heartbeats,
                    "nse_latency_ms": nse_lat,
                    "bse_latency_ms": bse_lat,
                    "avg_latency_ms": (nse_lat + bse_lat) / 2,
                    "portfolio_delta": health_report.get("portfolio_delta", {}),
                    "system_health": health_report.get("score", 1.0),
                    "source": "VM_CORE",
                    # [Layer 8] Micro-Signal Matrix Data for Indices
                    "index_states": {
                        asset: json.loads(await self.redis.get(f"latest_market_state:{asset}") or "{}")
                        for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]
                    },
                    "indicators": {
                        "iv_low_vol_trap": (float(await self.redis.get("iv_atm:NIFTY50") or 0.15) < 0.12),
                        "asto_shield": (abs(float(json.loads(await self.redis.get("latest_market_state:NIFTY50") or '{"asto":0}').get("asto", 0))) > 60)
                    }
                }

            @app.get("/analytics/meta_router_efficacy")
            async def get_meta_router_efficacy():
                """[Layer 8] Calculates Shadow P&L vs. Actual P&L for Counterfactual Analysis."""
                # [Audit 22.1] Anchor to IST to prevent date-line leakage for Indian markets
                now_ist = datetime.now(tz=IST)
                today_start = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
                
                # Phase 34: The MOC Noise Pollution Fence
                moc_cutoff = today_start.replace(hour=15, minute=24, second=59)
                upper_bound = moc_cutoff if now_ist > moc_cutoff else now_ist

                async with self.pool.acquire() as conn:
                    # 1. Fetch Actual Realized P&L (Opened & Closed Today Only, Fenced at 15:24:59)
                    # [Audit 23.5] Carry-Forward Bias Fix: created_at >= $1 ensures we only sum intraday alpha
                    actual_pnl_row = await conn.fetchrow(
                        "SELECT SUM(realized_pnl) as total FROM portfolio WHERE updated_at >= $1 AND updated_at <= $2 AND created_at >= $1", 
                        today_start, upper_bound
                    )
                    actual_pnl = float(actual_pnl_row['total'] or 0.0)
                    
                    # 2. Fetch All Shadow Intents (Pre-Veto) with Institutional Step 21.1 (Slippage Parity)
                    shadows = await conn.fetch(
                        "SELECT asset, action, quantity, execution_price, intent_price FROM shadow_trades WHERE time >= $1 AND time <= $2 AND status = 'COMPLETE'",
                        today_start, upper_bound
                    )
                    
                    shadow_pnl = 0.0
                    for s in shadows:
                        asset = s['asset']
                        # [PhD Critical] Prioritize execution_price (includes simulated slippage) over intent_price
                        entry_price = float(s['execution_price'] if s['execution_price'] is not None else s['intent_price'] or 0.0)
                        
                        # Get current price from Redis for Mark-to-Market
                        curr_price_raw = await self.redis.get(f"latest_market_state:{asset}")
                        curr_price = float(json.loads(curr_price_raw).get('price', entry_price)) if curr_price_raw else entry_price
                        
                        # Calculate hypothetical MTM P&L: (Exit - Entry) * Lots
                        if s['action'] == 'BUY':
                            shadow_pnl += (curr_price - entry_price) * s['quantity']
                        else:
                            shadow_pnl += (entry_price - curr_price) * s['quantity']
                            
                return {
                    "actual_pnl": actual_pnl,
                    "shadow_pnl": shadow_pnl,
                    "alpha_added": actual_pnl - shadow_pnl,
                    "intent_count": len(shadows),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }

            @app.get("/audit/rejections")
            async def get_rejections():
                """Phase 9.1: Exposes the 'Audit Journal' of risk vetoes."""
                if not self.pool: return []
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT time, asset, strategy_id, reason, alpha, vpin FROM rejections ORDER BY time DESC LIMIT 50"
                    )
                    return [dict(r) for r in rows]

            @app.get("/metrics")
            async def get_metrics():
                return {
                    "latency": float(await self.redis.get("SYSTEM_LATENCY_TEST") or 0),
                    "ops_per_sec": float(await self.redis.get("OPS_PER_SEC") or 0),
                    "buffer_usage": float(await self.redis.get("BUFFER_USAGE_PCT") or 0)
                }

            # Phase 40 API Isolations: While a separate uvicorn proc is ideal, 
            # state sharing for health_agg requires we maintain loop affinity.
            # Using async server to prevent main loop starvation.
            config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="error")
            server = uvicorn.Server(config)
            await server.serve()
        except Exception as e:
            logger.error(f"Metrics API error: {e}")


if __name__ == "__main__":
    if uvloop:
        uvloop.install()
    elif sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    controller = SystemController()
    asyncio.run(controller.start())
