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
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from core.alerts import send_cloud_alert
from core.mq import MQManager, Ports

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

# HMM Ingestion Hard Stop (to prevent MOC noise)
LOGGER_STOP_HH = 15
LOGGER_STOP_MM = 25

# Heuristic Engine parameters (Phase 0)
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
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self.redis: redis.Redis | None = None
        self._macro_events: list[dict] = []
        self._lockdown_announced: set[str] = set()  # event keys already locked
        self._preemption_detected = False
        self._shutdown_flag = False
        
        # Shoonya API for history fetch (Phase 0)
        from NorenRestApiPy.NorenApi import NorenApi
        host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
        ws_host = host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")
        self.api = NorenApi(host=host, websocket=ws_host)
        self.pool: asyncpg.Pool | None = None
        self._boot_time = time.time()

    # ── Startup ─────────────────────────────────────────────────────────────

    async def start(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        # [F10-01] Add DB connection retry loop
        retry_count = 0
        while True:
            try:
                self.pool = await asyncpg.create_pool(os.getenv("DB_DSN", "postgresql://user:pass@localhost/trading"), min_size=1, max_size=5, timeout=5.0)
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"SystemController DB connect failed (Attempt {retry_count}): {e}")
                await asyncio.sleep(min(5 * retry_count, 60))
        self._macro_events = self._load_macro_calendar()

        # ── Setup Hard Global Budget Constraint ──
        # Capital limits are now configured via the UI and stored in Redis.
        paper_limit = float(await self.redis.get("PAPER_CAPITAL_LIMIT") or 50000.0)
        live_limit = float(await self.redis.get("LIVE_CAPITAL_LIMIT") or 0.0)
        
        # --- Phase 3.1: Regulatory Capital Split (50:50 Cash-to-Collateral) ---
        # Capital limits are now split according to regulatory 'Cash Component' rules.
        eff_live = float(live_limit * (1 - HEDGE_RESERVE_PCT))
        res_live = float(live_limit * HEDGE_RESERVE_PCT)
        
        # Enforce 50:50 ratio on available margin
        cash_live = eff_live * 0.5
        coll_live = eff_live * 0.5
        
        await self.redis.set("CASH_COMPONENT_LIVE", f"{cash_live:.2f}")
        await self.redis.set("COLLATERAL_COMPONENT_LIVE", f"{coll_live:.2f}")
        await self.redis.set("HEDGE_RESERVE_LIVE", f"{res_live:.2f}")
        
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
        
        # No need for redundant GLOBAL_ prefix, standardize on PAPER/LIVE_CAPITAL_LIMIT
        await self.redis.set("PAPER_CAPITAL_LIMIT", paper_limit)
        await self.redis.set("LIVE_CAPITAL_LIMIT", live_limit)
        
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
            asyncio.create_task(self._preemption_poller())
            asyncio.create_task(self._macro_lockdown_watcher())
            asyncio.create_task(self._hmm_sync_watcher())
            asyncio.create_task(self._three_stage_eod_scheduler()) 
            asyncio.create_task(self._t1_calendar_sweep_watcher()) # Phase 3
            asyncio.create_task(self._unsettled_premium_quarantine()) # Phase 3.3
            asyncio.create_task(self._quarterly_settlement_guard())    # Phase 3.4
            asyncio.create_task(self._hard_state_sync())          # Spec 11.6: Ghost Fill Sync
            asyncio.create_task(self._exchange_health_monitor())   # Spec 12.4: NSE Halt Switch
            asyncio.create_task(self._daily_lookback_scheduler())
            
            # ── Phase 9: UI & Observability ──
            from core.health import HealthAggregator, HeartbeatProvider
            self.health_agg = HealthAggregator(self.redis)
            self.hb = HeartbeatProvider("SystemController", self.redis)
            asyncio.create_task(self.hb.run_heartbeat())
            asyncio.create_task(self._health_aggregation_loop())
            asyncio.create_task(self._run_metrics_api())

            logger.info("System Controller [Core 3] initialized and monitoring.")
            # Keep the main task alive indefinitely or until shutdown_flag is set
            while not self._shutdown_flag:
                await asyncio.sleep(1) # Sleep briefly to allow other tasks to run

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
                    asyncio.create_task(send_cloud_alert(
                        "⚡ GCP SPOT VM PREEMPTION DETECTED! Initiating batched SQUARE_OFF_ALL.",
                        alert_type="CRITICAL"
                    ))
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
                        row = await conn.fetchrow("""
                            SELECT
                                COUNT(*) FILTER (WHERE execution_type = 'Paper') AS paper_count,
                                COUNT(*) FILTER (WHERE execution_type = 'Live')  AS live_count
                            FROM trades
                            WHERE time >= CURRENT_DATE
                        """)
                        if row:
                            trade_count_paper = row["paper_count"] or 0
                            trade_count_live  = row["live_count"]  or 0

                        # Open positions (non-zero quantity)
                        positions = await conn.fetch("""
                            SELECT symbol, strategy_id, execution_type, quantity, avg_price, realized_pnl
                            FROM portfolio
                            WHERE quantity != 0
                            ORDER BY execution_type, symbol
                        """)
                        for p in positions:
                            open_positions.append(
                                f"  {'📄' if p['execution_type'] == 'Paper' else '🟢'} "
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

    # ── HMM & Data Synchronization ──────────────────────────────────────────

    async def _hmm_sync_watcher(self):
        """Manages HMM warm-up and data logger hard-stop signals."""
        logger.info("HMM synchronization watcher active.")
        while not self._shutdown_flag:
            now = datetime.now(tz=IST)
            
            # HMM_WARM_UP is deprecated in Heuristic Era
            await self.redis.set("HMM_WARM_UP", "False")
            
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
                
                # 3. 16:00 IST - HARD_VM_SHUTDOWN
                if now.hour == SHUTDOWN_HH and now.minute == SHUTDOWN_MM:
                    logger.critical("💀 16:00 IST: HARD SHUTDOWN sequence started.")
                    # ── EOD Summary Report (before square-off) ──
                    await self._eod_summary_report(now)
                    # POSITIONAL trades hibernate - no square_off_all here
                    logger.critical("💾 POSITIONAL trades hibernated in DB. Server shutting down.")
                    self._shutdown_flag = True
                
            except Exception as e:
                logger.error(f"EOD Scheduler error: {e}")
            
            await asyncio.sleep(60)

    async def _unsettled_premium_quarantine(self):
        """Phase 3.3: Prevents using option sell premium until T+1 settlement."""
        logger.info("Unsettled premium quarantine active. Monitoring T+1 credit.")
        while not self._shutdown_flag:
            try:
                # Find credit premium from today's sell orders
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow("""
                        SELECT SUM(realized_pnl) as today_credit 
                        FROM portfolio 
                        WHERE realized_pnl > 0 AND updated_at >= CURRENT_DATE
                    """)
                    if row and row['today_credit']:
                        credit = float(row['today_credit'])
                        # Move credit into a 'quarantine' bucket so it's not available in margin
                        # This effectively locks the premium until the next BOOT
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
            except zmq.Again:
                continue
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
                    async with self.pool.acquire() as conn:
                        # Find POSITIONAL positions expiring tomorrow
                        rows = await conn.fetch("""
                            SELECT DISTINCT parent_uuid, underlying 
                            FROM portfolio 
                            WHERE has_calendar_risk = TRUE 
                              AND lifecycle_class = 'POSITIONAL' 
                              AND expiry_date = CURRENT_DATE + INTERVAL '1 day'
                              AND quantity != 0
                        """)
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
                if i + SEBI_BATCH_SIZE < len(rows):
                    logger.info(f"SEBI Batch complete ({len(batch)} orders). Waiting {INTER_BATCH_WAIT}s...")
                    await asyncio.sleep(INTER_BATCH_WAIT)

    async def _daily_lookback_scheduler(self):
        """Schedules and executes the 14-day daily close fetch at 09:00 IST."""
        logger.info("Daily lookback scheduler active.")
        while not self._shutdown_flag:
            now = datetime.now(tz=IST)
            
            # Only run at HISTORY_FETCH_HH:HISTORY_FETCH_MM
            if now.hour == HISTORY_FETCH_HH and now.minute == HISTORY_FETCH_MM:
                logger.info("🕒 Triggering 14-day historical fetch for RV/ADX...")
                await self._fetch_14d_history()
                # Wait 65s to avoid double trigger
                await asyncio.sleep(65)
            
            await asyncio.sleep(30)

    async def _fetch_14d_history(self):
        """Fetches 14 days of history for Nifty & BankNifty from Shoonya."""
        try:
            # Login only when needed for history fetch
            user = os.getenv("SHOONYA_USER")
            pwd = os.getenv("SHOONYA_PWD")
            factor2 = os.getenv("SHOONYA_FACTOR2")
            vc = os.getenv("SHOONYA_VC")
            app_key = os.getenv("SHOONYA_APP_KEY")
            imei = os.getenv("SHOONYA_IMEI")
            
            import pyotp
            totp = pyotp.TOTP(factor2).now()
            self.api.login(userid=user, password=pwd, twoFA=totp, vendor_code=vc, api_secret=app_key, imei=imei)
            
            for symbol in ["NSE|26000", "NSE|26001", "BSE|1"]: # Nifty, BankNifty, Sensex
                # Fetch daily candles for the last 14 sessions
                end_time = datetime.now().timestamp()
                start_time = end_time - (20 * 86400) # 20 days to ensure 14 trading sessions
                
                # Fetch time series
                ret = self.api.get_time_price_series(exchange=symbol.split('|')[0], 
                                                   token=symbol.split('|')[1], 
                                                   starttime=start_time, 
                                                   endtime=end_time, 
                                                   interval=None) # None for daily candles
                
                if ret and isinstance(ret, list):
                    # Sort and take last 14
                    ret.sort(key=lambda x: x['time'])
                    closes = [float(c['ssoc']) for c in ret[-LOOKBACK_DAYS:]]
                    
                    asset_map = {"26000": "NIFTY50", "26001": "BANKNIFTY", "1": "SENSEX"}
                    asset_name = asset_map.get(symbol.split('|')[1], "UNKNOWN")
                    await self.redis.set(f"history_14d:{asset_name}", json.dumps(closes))
                    logger.info(f"✅ Stored 14D history for {asset_name}: {len(closes)} bars.")
            
            logger.info("14-day lookback sync complete.")
        except Exception as e:
            logger.error(f"Error fetching 14d history: {e}")

    # ── Hard State Sync ──────────────────────────────────────────────────────

    async def _hard_state_sync(self):
        """Every 15 minutes, compares broker positions with DB 'portfolio' table."""
        logger.info("Ghost Fill Sync task started (15m interval).")
        while not self._shutdown_flag:
            try:
                # 1. Fetch broker truth
                loop = asyncio.get_running_loop()
                broker_positions = await loop.run_in_executor(None, self.api.get_positions)
                
                if broker_positions is None: # Possible on error
                    await asyncio.sleep(900)
                    continue
                
                # Convert list of dicts to a map {symbol: qty}
                truth_map = {}
                if isinstance(broker_positions, list):
                    for pos in broker_positions:
                        if int(pos.get('netqty', 0)) != 0:
                            truth_map[pos['tsym']] = int(pos['netqty'])
                
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
                            if t_qty != 0 and d_qty == 0:
                                # Orphan detected: Broker has it, we don't.
                                logger.critical(f"🚨 ORPHAN POSITION DETECTED: {sym} | Broker: {t_qty}, DB: {d_qty}")
                                await self.redis.publish("panic_channel", json.dumps({
                                    "action": "ORPHAN_LIQUIDATE",
                                    "symbol": sym,
                                    "qty": t_qty,
                                    "reason": "GHOST_SYNC_GAP"
                                }))
                                await send_cloud_alert(f"🚨 ORPHAN POSITION: Broker sees {t_qty} of {sym} but DB is flat. Liquidation triggered.", alert_type="CRITICAL")
                            
                            elif t_qty == 0 and d_qty != 0:
                                # DB ghost detected: We think we have it, broker says flat.
                                logger.warning(f"👻 STALE GHOST DETECTED: {sym} | Cleaning DB state.")
                                await conn.execute("UPDATE portfolio SET quantity = 0, avg_price = 0 WHERE symbol = $1", sym)
                                await send_cloud_alert(f"👻 STALE GHOST: DB showed {d_qty} for {sym} but broker is flat. DB cleaned.", alert_type="WARNING")

            except Exception as e:
                logger.error(f"Ghost sync error: {e}")
            
            await asyncio.sleep(900) # 15 minutes

    async def _periodic_margin_sync(self):
        """[Audit-Fix] Additive: Periodically pulls live margin limits from Shoonya and updates Redis."""
        logger.info("Broker margin sync task active (60s interval).")
        while not self._shutdown_flag:
            try:
                # Only sync if we have a valid API session (login happens in _fetch_14d_history or on-demand)
                # For this additive fix, we'll login if needed or reuse existing
                if not hasattr(self, 'api') or not self.api:
                    await asyncio.sleep(10)
                    continue

                loop = asyncio.get_running_loop()
                limits = await loop.run_in_executor(None, self.api.get_limits)
                
                if limits and limits.get('stat') == 'Ok':
                    # Shoonya returns 'cash' and 'collateral' in 'cash' field or similar depending on broker setup
                    # Usually 'cash' is available margin. We'll map it to our internal keys.
                    total_cash = float(limits.get('cash', 1000000.0))
                    
                    # Update Redis for MetaRouter to consume
                    # We apply the HEDGE_RESERVE_PCT here to maintain the safety buffer
                    eff_cash = total_cash * (1 - HEDGE_RESERVE_PCT)
                    await self.redis.set("CASH_COMPONENT_LIVE", f"{eff_cash * 0.5:.2f}")
                    await self.redis.set("COLLATERAL_COMPONENT_LIVE", f"{eff_cash * 0.5:.2f}")
                    await self.redis.set("HEDGE_RESERVE_LIVE", f"{total_cash * HEDGE_RESERVE_PCT:.2f}")
                    
                    logger.debug(f"📊 Margin Sync: TotalCash={total_cash:.2f} | Eff={eff_cash:.2f}")
                
            except Exception as e:
                logger.error(f"Broker margin sync failed: {e}")
            
            await asyncio.sleep(60) # 60 second interval

    # ── Exchange Health Monitor (Spec 12.4) ──────────────────────────────────

    async def _exchange_health_monitor(self):
        """Phase 12.2: High-Precision Circuit Breaker (Auto-Halt)."""
        logger.info("Exchange health monitor active. Threshold: 500ms / 3 missing ticks.")
        missing_ticks = 0
        
        while not self._shutdown_flag:
            try:
                # Only monitor during active market hours
                now = datetime.now(tz=IST)
                if not (now.hour == 9 and now.minute >= 15) and not (now.hour > 9 and now.hour < 15) and not (now.hour == 15 and now.minute <= 30):
                    await asyncio.sleep(60)
                    continue

                # Check age of the latest NIFTY50 tick
                tick_raw = await self.redis.get("latest_tick:NIFTY50")
                if tick_raw:
                    tick = json.loads(tick_raw)
                    tick_ts_str = tick.get("timestamp") # ISO format
                    if tick_ts_str:
                        tick_ts = datetime.fromisoformat(tick_ts_str).astimezone(timezone.utc)
                        now_utc = datetime.now(timezone.utc)
                        latency_ms = (now_utc - tick_ts).total_seconds() * 1000
                        
                        # Phase 12.2: > 500ms jitter or stale feed
                        if latency_ms > 500:
                            missing_ticks += 1
                            logger.warning(f"⚠️ Feed Latency Spike: {latency_ms:.0f}ms | Missing: {missing_ticks}")
                        else:
                            missing_ticks = 0
                else:
                    missing_ticks += 1

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

    # ── Phase 9: Observability Helpers ────────────────────────────────────────

    async def _health_aggregation_loop(self):
        """Periodically computes system health score based on heartbeats."""
        while not self._shutdown_flag:
            try:
                health = await self.health_agg.get_system_health()
                await self.redis.set("SYSTEM_HEALTH_REPORT", json.dumps(health, cls=NumpyEncoder))
                # Update high-level flag for metrics API
                await self.redis.set("SYSTEM_HEALTH_SCORE", f"{health['score']:.2f}")
            except Exception as e:
                logger.error(f"Health aggregation failed: {e}")
            await asyncio.sleep(10)

    async def _run_metrics_api(self):
        """Lightweight REST endpoint for dashboard metrics (Spec 9.1)."""
        try:
            from fastapi import FastAPI
            import uvicorn
            
            app = FastAPI(title="ControllerMetrics")

            @app.get("/health")
            async def get_health():
                raw = await self.redis.get("SYSTEM_HEALTH_REPORT")
                return json.loads(raw) if raw else {"status": "starting"}

            @app.get("/metrics")
            async def get_metrics():
                return {
                    "latency": float(await self.redis.get("SYSTEM_LATENCY_TEST") or 0),
                    "ops_per_sec": float(await self.redis.get("OPS_PER_SEC") or 0),
                    "buffer_usage": float(await self.redis.get("BUFFER_USAGE_PCT") or 0)
                }

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
