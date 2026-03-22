"""
Automated Liquidation & Risk Guardian (Refactored)
Thin orchestrator using Strategy Pattern barriers, PortfolioState, and Risk Math.
All domain logic delegated to core/risk/ and core/math/ modules.
"""

import asyncio
import json
import logging
import sys
import time
import uuid
import re
from datetime import datetime, timezone, timedelta
import os
import math
import zmq
import zmq.asyncio

import redis.asyncio as redis
import asyncpg
from dotenv import load_dotenv
load_dotenv()

from core.db_retry import with_db_retry
from core.greeks import BlackScholes
from core.shm import ShmManager, SignalVector, RegimeShm, RegimeVector, is_shm_missing
from core.mq import MQManager, Ports, Topics
from core.math.risk_utils import (
    determine_underlying, calculate_pnl, check_velocity_breach,
    calculate_dynamic_slippage_budget, SLIPPAGE_HALT_TTL_SEC,
    CVD_FLIP_EXIT_THRESHOLD,
)
from core.risk.barriers import (
    BaseBarrier, KineticBarrier, PositionalBarrier, ZeroDteBarrier,
    BarrierContext, BarrierResult,
)
from core.risk.portfolio_state import PortfolioState
from core.alerts import send_cloud_alert

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LiquidationDaemon")

# HTTP 400 emsg substrings that indicate CIRCUIT LIMIT (safe to re-fire)
CIRCUIT_LIMIT_STRINGS = ["price range", "circuit", "freeze", "price band", "pcl", "ucl"]
# emsg substrings that indicate ABORT (don't re-fire)
ABORT_STRINGS = ["margin", "insufficient", "invalid", "malformed", "not found", "order not found"]


class LiquidationDaemon:
    def __init__(self):
        self.mq = MQManager()
        self.order_pub = self.mq.create_push(Ports.ORDERS, bind=False)

        self._redis: redis.Redis | None = None
        self.pool: asyncpg.Pool | None = None
        self.portfolio: PortfolioState | None = None

        # Barrier Strategy Registry
        self.barrier_registry: dict[str, BaseBarrier] = {
            "KINETIC": KineticBarrier(),
            "ZERO_DTE": ZeroDteBarrier(),
            "POSITIONAL": PositionalBarrier(),
        }

        # [Audit-Fix] Dual-Vector SHM Surveillance
        self.shm_alpha_managers = {
            idx: ShmManager(asset_id=idx, mode='r') for idx in ["NIFTY50", "BANKNIFTY", "SENSEX"]
        }
        self.shm_regime_managers = {
            idx: RegimeShm(asset_id=idx, mode='r') for idx in ["NIFTY50", "BANKNIFTY", "SENSEX"]
        }
        self.shm_alpha_managers["GLOBAL"] = ShmManager(asset_id="GLOBAL", mode='r')

        # Risk State Buffers
        self.last_red_alert_ts: dict[str, float] = {}
        self.last_tsl_sync_ts: dict[str, float] = {}

    # ── Startup ──────────────────────────────────────────────────────────────

    async def run(self):
        from core.auth import get_redis_url, get_db_dsn
        self._redis = redis.from_url(get_redis_url(), decode_responses=True)
        self.pool = await asyncpg.create_pool(get_db_dsn(), min_size=1, max_size=5)

        # Initialize Portfolio State Manager
        self.portfolio = PortfolioState(self._redis, self.pool)
        await self.portfolio.hydrate_from_db()

        logger.info("LiquidationDaemon active. Three-barrier system armed.")

        await asyncio.gather(
            self._handoff_listener(),
            self._position_alert_listener(),
            self._market_monitor(),
            self._monitor_fill_slippage(),
            self._run_heartbeat(),
            self.portfolio.periodic_sync(interval=60),
        )

    async def _position_alert_listener(self):
        """Listens for NEW_POSITION_ALERTS — event-driven arming."""
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe("NEW_POSITION_ALERTS")
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    symbol = data.get("symbol", "")

                    # Event-driven: arm instantly from the alert payload
                    if data.get("quantity") and data.get("price"):
                        await self.portfolio.arm_from_event(data)
                    else:
                        # Fallback: hydrate from DB (legacy path)
                        await asyncio.sleep(0.5)
                        await self.portfolio.hydrate_specific(symbol)

                    # Ensure JIT Feed is active
                    await self._redis.publish("dynamic_subscriptions", f"NFO|{symbol}")
                except Exception as e:
                    logger.error(f"Error processing NEW_POSITION_ALERT: {e}")
        except asyncio.CancelledError:
            pass

    async def _run_heartbeat(self):
        from core.health import HeartbeatProvider
        hb = HeartbeatProvider("LiquidationDaemon", self._redis)
        await hb.run_heartbeat()

    # ── Slippage Monitor ─────────────────────────────────────────────────────

    async def _monitor_fill_slippage(self):
        """Monitors trade execution quality using dynamic slippage budgets."""
        try:
            pubsub = self._redis.pubsub()
            await pubsub.subscribe("order_confirmations")
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    fill_price = float(data.get("fill_price") or 0)
                    intended_price = float(data.get("intended_price") or fill_price)
                    symbol = data.get("symbol", "UNKNOWN")

                    if intended_price > 0 and fill_price > 0:
                        rv = float(await self._redis.get("rv") or 0.0)
                        dynamic_budget = calculate_dynamic_slippage_budget(rv)

                        slip = abs(fill_price - intended_price) / intended_price
                        if slip > dynamic_budget:
                            await self._redis.set("SLIPPAGE_HALT", "True", ex=SLIPPAGE_HALT_TTL_SEC)
                            logger.critical(
                                f"🚨 SLIPPAGE HALT [{symbol}]: {slip:.1%}/{dynamic_budget:.1%} "
                                f"(fill={fill_price:.2f} vs intended={intended_price:.2f}). "
                                f"Pausing new entries for {SLIPPAGE_HALT_TTL_SEC}s."
                            )
                            asyncio.create_task(send_cloud_alert(
                                f"🚨 SLIPPAGE BUDGET BREACHED\n"
                                f"Symbol: {symbol} | Slippage: {slip:.1%}\n"
                                f"Fill: ₹{fill_price:.2f} vs Intended: ₹{intended_price:.2f}\n"
                                f"New entries paused for {SLIPPAGE_HALT_TTL_SEC}s.",
                                alert_type="RISK"
                            ))
                except Exception as e:
                    logger.error(f"Slippage monitor parse error: {e}")
        except asyncio.CancelledError:
            pass

    # ── Handoff Listener ─────────────────────────────────────────────────────

    async def _handoff_listener(self):
        """Listens for ORPHAN/HANDOFF commands from strategy engine."""
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["ORPHAN", "HANDOFF"])
        while True:
            try:
                _, msg = await self.mq.recv_json(sub)
                if msg:
                    symbol = msg.get("symbol", "NIFTY50")
                    msg["entry_time"] = time.time()
                    msg["stall_retries"] = 0
                    self.portfolio.positions[symbol] = msg
                    logger.info(
                        f"Accepted ORPHAN: {symbol} Qty={msg.get('quantity')} "
                        f"Entry={msg.get('price', 0):.2f}"
                    )
            except Exception as e:
                logger.error(f"Handoff listener error: {e}")
            await asyncio.sleep(0.05)

    # ── Market Monitor (Lean Orchestrator) ────────────────────────────────────

    async def _market_monitor(self):
        """Polls market state and tick data. Delegates barrier evaluation to Strategy classes."""
        asyncio.create_task(send_cloud_alert(
            "🛡️ LIQUIDATION DAEMON: Active and monitoring risk thresholds.", alert_type="SYSTEM"
        ))

        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK."])
        state_sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE"])

        latest_state: dict = {}

        async def _state_poller():
            nonlocal latest_state
            while True:
                try:
                    _, state = await self.mq.recv_json(state_sub)
                    if state:
                        latest_state = state
                except zmq.Again:
                    pass
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        asyncio.create_task(_state_poller())

        while True:
            try:
                topic, tick = await self.mq.recv_json(market_sub)

                if topic and tick and self.portfolio.has_positions():
                    incoming_symbol = str(topic).split(".")[-1]

                    # 1. Index tick: check day loss + hybrid drawdown + related positions
                    if incoming_symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                        await self._check_stop_day_loss()
                        await self._check_hybrid_drawdown()

                        for pos_symbol, pos in list(self.portfolio.positions.items()):
                            underlying = determine_underlying(pos_symbol)
                            if underlying == incoming_symbol:
                                await self._evaluate_barriers(pos_symbol, tick, latest_state, is_index_tick=True)

                    # 2. Evaluate barriers for the SPECIFIC symbol that just ticked
                    if incoming_symbol in self.portfolio.positions:
                        await self._evaluate_barriers(incoming_symbol, tick, latest_state, is_index_tick=False)

            except zmq.Again:
                await asyncio.sleep(0.01)
                continue
            except Exception as e:
                logger.error(f"Market monitor error: {e}")
                await asyncio.sleep(0.5)

    # ── Barrier Evaluation (5-Gate + Strategy Dispatch) ────────────────────────

    async def _evaluate_barriers(self, symbol: str, tick: dict, state: dict, is_index_tick: bool = False):
        pos = self.portfolio.get_position(symbol)
        if not pos:
            return

        underlying = determine_underlying(symbol)

        # 1. Read SHM vectors
        sig_shm = self.shm_alpha_managers.get(underlying)
        reg_shm = self.shm_regime_managers.get(underlying)
        sig = sig_shm.read_dict() if sig_shm else {}
        reg = reg_shm.read() if reg_shm else {}

        price = float(tick.get("price", 0.0))
        entry = float(pos.get("price", price))
        pnl = calculate_pnl(price, entry, pos.get("action", "BUY"))

        s18 = reg.get("regime_s18", 0)
        s27 = reg.get("quality_s27", 100.0)

        # ── GATE 2: Regime Shift (S18 = 3) → Pre-emptive Reduction ────────
        if s18 == 3 and pos.get("lifecycle_class") == "POSITIONAL":
            parent_uuid = pos.get("parent_uuid")
            exit_reason = f"PRE_EMPTIVE_REDUCTION: S18=3 (Volatile Regime Shift)"
            await self._execute_basket_reduction(parent_uuid, 0.50, exit_reason)
            return

        # ── GATE 3: Hedge Activation (ASTO ±90) ──────────────────────────
        asto = sig.get("asto", 0.0) if not is_shm_missing(sig.get("asto", 0.0)) else 0.0
        if abs(asto) >= 90:
            delta = sig.get("net_delta", 0.0) if not is_shm_missing(sig.get("net_delta", 0.0)) else 0.0
            rv = sig.get("rv", 0.0) if not is_shm_missing(sig.get("rv", 0.0)) else 0.0
            await self._execute_hedge_waterfall(pos, delta, rv)

        # ── GATE 4: Quality Decay (S27 < 30) ─────────────────────────────
        if s27 < 30 and pnl > 0 and pos.get("lifecycle_class") == "KINETIC":
            exit_reason = f"PROFIT_BANK: S27 Quality={s27:.1f} < 30"
            await self._record_barrier_exit(symbol, "QUALITY_DECAY", exit_reason, price, entry)
            await self._attempt_exit(pos, symbol, price, exit_reason)
            return

        # ── Velocity Exit (Enhancement 4) ─────────────────────────────────
        slope_15m_raw = sig.get("slope_15m", 0.0)
        slope_1m = float(slope_15m_raw) / 15.0 if not is_shm_missing(slope_15m_raw) else 0.0
        avg_slope_raw = await self._redis.get(f"AVG_SLOPE:{underlying}")
        avg_slope = float(avg_slope_raw or 0.0)

        if check_velocity_breach(slope_1m, avg_slope, pnl):
            exit_reason = f"VELOCITY_EXIT: Speed {slope_1m:.2f} > 2x Avg {avg_slope:.2f}"
            await self._record_barrier_exit(symbol, "VELOCITY", exit_reason, price, entry)
            await self._attempt_exit(pos, symbol, price, exit_reason)
            return

        # ── GATE 5: Time Gate (15:15 IST) ─────────────────────────────────
        now_ist = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30)))
        if now_ist.hour == 15 and now_ist.minute >= 15:
            exit_reason = "EOD_SQUARE_OFF: Market Closing (15:15 IST)"
            await self._record_barrier_exit(symbol, "TIME_LIMIT", exit_reason, price, entry)
            await self._attempt_exit(pos, symbol, price, exit_reason)
            return

        # ── Build BarrierContext ──────────────────────────────────────────
        atr_raw = sig.get("atr", 0.0)
        atr = float(atr_raw) if not is_shm_missing(atr_raw) else 20.0
        vix_raw = await self._redis.get(f"vix:{underlying}") or await self._redis.get("vix")
        vix = float(vix_raw) if vix_raw else 15.0
        rv_raw = sig.get("rv", 0.0)
        rv = float(rv_raw) if not is_shm_missing(rv_raw) else 0.0
        current_hmm = str(reg.get("regime_s18", 0))

        # SHM sentinel pattern: only fallback to Redis when SHM reports MISSING (NaN)
        cvd_raw = sig.get("cvd_score", 0.0)
        if is_shm_missing(cvd_raw):
            cvd_redis = await self._redis.get(f"cvd_flip_ticks:{underlying}") or await self._redis.get(f"cvd_score:{underlying}")
            cvd = float(cvd_redis or 0.0)
        else:
            cvd = float(cvd_raw)

        s_total_raw = sig.get("s_total", 0.0)
        if is_shm_missing(s_total_raw):
            s_total_redis = await self._redis.get(f"COMPOSITE_ALPHA:{underlying}") or await self._redis.get(f"s_total:{underlying}")
            s_total = float(s_total_redis or 0.0)
        else:
            s_total = float(s_total_raw)

        asto_raw = sig.get("asto", 0.0)
        if is_shm_missing(asto_raw):
            asto_redis = await self._redis.get(f"asto:{underlying}") or await self._redis.get("asto")
            asto_val = float(asto_redis or 0.0)
        else:
            asto_val = float(asto_raw)

        hurst_raw = await self._redis.get("hurst") or await self._redis.get("hurst_exponent")
        hurst = float(hurst_raw or 0.5)

        # Stagnation data
        recent_price_range = float('inf')
        has_range_data = False
        try:
            recent_prices_raw = await self._redis.lrange(f"tick_history:{symbol}", -50, -1)
            if recent_prices_raw and len(recent_prices_raw) >= 10:
                recent_prices = [float(p) for p in recent_prices_raw]
                recent_price_range = max(recent_prices) - min(recent_prices)
                has_range_data = True
        except Exception:
            pass

        # Inception spot for Zero-DTE
        inception_spot = float(pos.get("inception_spot", 0.0))
        if inception_spot <= 0:
            state_data = await self._redis.hget(f"POSITION_STATE:{symbol}", "inception_spot")
            if state_data:
                inception_spot = float(state_data)
                self.portfolio.update_position(symbol, {"inception_spot": inception_spot})

        m_state_raw = await self._redis.get("MARKET_STATE")
        market_state = str(m_state_raw or "")

        ctx = BarrierContext(
            price=price,
            is_index_tick=is_index_tick,
            entry=entry,
            action=pos.get("action", "BUY"),
            elapsed=time.time() - float(pos.get("entry_time", time.time())),
            lifecycle_class=str(pos.get("lifecycle_class") or "KINETIC"),
            parent_uuid=pos.get("parent_uuid", ""),
            initial_credit=float(pos.get("initial_credit", 0.0)),
            strategy_id=str(pos.get("strategy_id", "")),
            expiry_date=pos.get("expiry_date"),
            short_strikes=pos.get("short_strikes", {}),
            inception_spot=inception_spot,
            runner_active=pos.get("runner_active", False),
            local_high=float(pos.get("local_high", 0.0)),
            best_price=float(pos.get("best_price", 0.0)),
            entry_hmm=pos.get("entry_hmm", ""),
            dte=float(pos.get("dte", 7.0)) if pos.get("dte") is not None else 7.0,
            atr=atr,
            vix=vix,
            rv=rv,
            regime_s18=s18,
            quality_s27=s27,
            asto=asto_val,
            cvd_score=cvd,
            s_total=s_total,
            slope_15m=float(sig.get("slope_15m", 0.0)) if sig else 0.0,
            avg_slope=avg_slope,
            iv_rv_spread=float(sig.get("iv_rv_spread", 0.0)) if sig else 0.0,
            iv_atm=float(sig.get("iv_atm", 20.0)) if sig else 20.0,
            net_delta=float(sig.get("net_delta", 0.0)) if sig else 0.0,
            current_hmm=current_hmm,
            hurst=hurst,
            recent_price_range=recent_price_range,
            has_range_data=has_range_data,
            market_state=market_state,
            sl_price=float(pos.get("sl_price", 0.0)),
            tp1_price=float(pos.get("tp1_price", 0.0)),
            tp2_price=float(pos.get("tp2_price", 0.0)),
        )

        # ── Risk Telemetry ────────────────────────────────────────────────
        try:
            status = "GREEN"
            if pnl < -atr:
                status = "YELLOW"
            if s18 == 3:
                status = "RED"
            await self._redis.hset(f"POSITION_STATE:{symbol}", mapping={
                "uuid": pos.get("parent_uuid", "NONE"),
                "risk_status": status,
                "current_sl": pos.get("sl_level", entry - atr),
                "regime_s18": s18,
                "quality_s27": s27,
                "last_update": time.time()
            })
        except Exception:
            pass

        # ── Strategy Dispatch ─────────────────────────────────────────────
        lifecycle = ctx.lifecycle_class
        strategy = self.barrier_registry.get(lifecycle, self.barrier_registry["KINETIC"])
        result: BarrierResult = strategy.evaluate(pos, ctx)

        # Apply state mutations back to position
        if result.state_updates:
            self.portfolio.update_position(symbol, result.state_updates)

        # Handle hedge actions (non-exit)
        if result.hedge_action:
            await self._execute_hedge_waterfall(
                result.hedge_action["pos"],
                result.hedge_action["delta"],
                result.hedge_action["rv"],
            )

        # Handle exit triggers
        if result.triggered:
            await self._record_barrier_exit(symbol, result.barrier_type, result.reason, price, entry)
            if result.is_partial:
                await self._attempt_partial_exit(pos, symbol, price, result.reason, result.partial_pct)
                if result.state_updates.get("runner_active"):
                    logger.info(f"🏃 RUNNER ACTIVATED for {symbol} after Partial Exit TP1")
            else:
                await self._attempt_exit(pos, symbol, price, result.reason)

    # ── Stop Day Loss Guard ───────────────────────────────────────────────────

    async def _check_stop_day_loss(self):
        """Compares today's cumulative P&L against STOP_DAY_LOSS limit."""
        try:
            stop_limit = float(await self._redis.get("STOP_DAY_LOSS") or 16000.0)

            for mode_suffix in ["PAPER", "LIVE"]:
                day_pnl = float(await self._redis.get(f"DAILY_REALIZED_PNL_{mode_suffix}") or 0.0)
                unrealized = float(await self._redis.get(f"DAILY_UNREALIZED_PNL_{mode_suffix}") or 0.0)
                total_pnl = day_pnl + unrealized
                breach_key = f"STOP_DAY_LOSS_BREACHED_{mode_suffix}"

                if total_pnl <= -float(stop_limit):
                    already_breached = await self._redis.get(breach_key) == "True"
                    if not already_breached:
                        await self._redis.set(breach_key, "True")
                        asyncio.create_task(send_cloud_alert(
                            f"🛑 STOP DAY LOSS BREACHED [{mode_suffix}]: "
                            f"Daily P&L ₹{day_pnl:,.0f} <= -₹{stop_limit:,.0f}. "
                            f"Blocking new entries and triggering full liquidation.",
                            alert_type="CRITICAL"
                        ))
                        await self._redis.publish("panic_channel", json.dumps({
                            "action": "SQUARE_OFF_ALL",
                            "reason": f"STOP_DAY_LOSS_BREACHED_{mode_suffix}",
                            "execution_type": mode_suffix.capitalize()
                        }))
                else:
                    await self._redis.set(breach_key, "False")
        except Exception as e:
            logger.error(f"Stop Day Loss check error: {e}")

    async def _check_hybrid_drawdown(self):
        """Aggregates P&L across all legs of the same parent_uuid."""
        try:
            hybrids: dict[str, list[dict]] = {}
            for sym, pos in self.portfolio.positions.items():
                p_uuid = pos.get("parent_uuid")
                if p_uuid:
                    if p_uuid not in hybrids:
                        hybrids[p_uuid] = []
                    hybrids[p_uuid].append(pos)

            limit = float(await self._redis.get("HYBRID_DRAWDOWN_LIMIT") or 2500.0)

            for p_uuid, legs in hybrids.items():
                total_pnl = 0.0
                total_qty = 0
                for leg in legs:
                    sym = leg["symbol"]
                    entry = float(leg.get("price", 0.0))
                    qty = float(leg.get("quantity", 0))
                    p_raw = await self._redis.get(f"latest_price:{sym}")
                    if not p_raw:
                        continue
                    price = float(p_raw)
                    action = leg.get("action", "SELL")
                    total_pnl += calculate_pnl(price, entry, action) * abs(qty)
                    if "FUT" not in sym:
                        total_qty += abs(qty)

                # Index-aware scaling
                sample_leg = legs[0]
                underlying = determine_underlying(sample_leg.get("symbol", ""))
                lot_size_raw = await self._redis.hget("lot_sizes", underlying)
                idx_lot_size = int(lot_size_raw) if lot_size_raw else 50
                num_lots = max(1, int(total_qty // idx_lot_size))
                dynamic_limit = float(limit * num_lots)

                if total_pnl <= -dynamic_limit:
                    logger.critical(f"🛑 HYBRID DRAWDOWN: Parent {p_uuid} P&L ₹{total_pnl:.0f} <= -₹{dynamic_limit:.0f}")
                    asyncio.create_task(send_cloud_alert(
                        f"🛑 HYBRID DRAWDOWN BREACH\nParent: {p_uuid}\nP&L: ₹{total_pnl:,.0f}\n"
                        f"Triggering full liquidation for all related legs.",
                        alert_type="CRITICAL"
                    ))
                    for leg in legs:
                        await self._attempt_exit(leg, leg["symbol"], 0, f"HYBRID_DRAWDOWN: {total_pnl:.0f}")
        except Exception as e:
            logger.error(f"Hybrid Drawdown check error: {e}")

    # ── Hedge Waterfall ──────────────────────────────────────────────────────

    async def _execute_hedge_waterfall(self, pos: dict, delta: float, rv: float):
        """Hedge Waterfall Protocol (Spec 11.4)."""
        symbol = pos['symbol']
        logger.warning(f"🌊 Waterfall Protocol for {symbol} | Delta={delta:.2f} | RV={rv:.4f}")

        if abs(delta) > 0.30 or rv > 0.003:
            exit_reason = f"WATERFALL_PANIC: Delta {delta:.2f} or RV {rv:.4f} too high."
            await self._attempt_exit(pos, symbol, 0, exit_reason)
            return

        # Moderate breach: Dispatch neutralizing Hedge Order
        hedge_action = "BUY" if delta < 0 else "SELL"
        lot_size_raw = await self._redis.hget("lot_sizes", pos.get("underlying", "NIFTY50"))
        lot_size = int(lot_size_raw) if lot_size_raw else 50
        hedge_qty = max(1, math.ceil(abs(delta) * lot_size))

        hedge_order = {
            "order_id": f"hedge_{uuid.uuid4().hex[:8]}",
            "symbol": f"{pos.get('underlying', 'NIFTY50')}-FUT",
            "action": hedge_action,
            "quantity": hedge_qty,
            "order_type": "MARKET",
            "strategy_id": "DELTA_HEDGE",
            "execution_type": pos.get("execution_type", "Paper"),
            "parent_uuid": pos.get("parent_uuid"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": f"WATERFALL_HEDGE: Delta={delta:.2f}"
        }

        try:
            await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, hedge_order)
            logger.info(f"🛡️ STEP 1 HEDGE DISPATCHED: {hedge_action} {hedge_qty} Micro-FUT for {symbol}")
            self.portfolio.update_position(symbol, {
                "last_hedge_ts": time.time(),
                "net_delta_at_hedge": delta,
            })
        except Exception as e:
            logger.error(f"Step 1 hedge dispatch failure: {e}")

        # Step 2: Roll if margin insufficient
        cash_raw = await self._redis.get("CASH_COMPONENT_LIVE")
        coll_raw = await self._redis.get("COLLATERAL_COMPONENT_LIVE")
        margin_avail = float(cash_raw or 0) + float(coll_raw or 0)
        if margin_avail < 10000:
            logger.warning(f"🔄 WATERFALL STEP 2: Margin insufficient ({margin_avail:.0f}). Attempting IC Roll.")
            roll_side = "PE" if delta > 0 else "CE"
            roll_order = {
                "order_id": f"roll_{uuid.uuid4().hex[:8]}",
                "symbol": symbol,
                "action": "ROLL",
                "side": roll_side,
                "parent_uuid": pos.get("parent_uuid"),
                "strategy_id": "DELTA_ROLL",
                "execution_type": pos.get("execution_type", "Paper"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": f"WATERFALL_ROLL: Delta={delta:.2f}, margin={margin_avail:.0f}"
            }
            try:
                await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, roll_order)
                logger.info(f"🔄 ROLL DISPATCHED: {roll_side} wing for {symbol}")
            except Exception as e:
                logger.error(f"Step 2 roll failure: {e}")

        # Step 3: Pro-rata slash if extreme
        total_cap = float(await self._redis.get("LIVE_CAPITAL_LIMIT") or 800000)
        cash_avail = float(await self._redis.get("CASH_COMPONENT_LIVE") or 0)
        coll_avail = float(await self._redis.get("COLLATERAL_COMPONENT_LIVE") or 0)
        margin_available = cash_avail + coll_avail
        margin_util = max(0, total_cap - margin_available)
        if abs(delta) >= 0.25 or (total_cap > 0 and (margin_util / total_cap) > 0.90):
            logger.warning(f"🔪 WATERFALL STEP 3: Pro-rata 25% slash. Delta={delta:.2f}")
            await self._attempt_partial_exit(pos, symbol, 0, f"PRORATA_SLASH: Delta={delta:.2f}", 0.25)

    async def _execute_basket_reduction(self, parent_uuid: str, pct: float, reason: str):
        """Reduces all legs in a position basket by a specific percentage."""
        if not parent_uuid:
            return
        basket_legs = self.portfolio.get_basket(parent_uuid)
        logger.warning(f"🔪 BASKET REDUCTION: {parent_uuid} | Pct={pct:.0%} | Reason={reason}")
        for leg in basket_legs:
            await self._attempt_partial_exit(leg, leg["symbol"], 0, reason, pct)

    # ── Exit Execution ────────────────────────────────────────────────────────

    async def _attempt_partial_exit(self, pos: dict, symbol: str, price: float, reason: str, pct: float):
        qty = abs(pos.get("quantity", 0))
        exit_qty = int(qty * pct)
        if exit_qty == 0 or exit_qty == qty:
            await self._attempt_exit(pos, symbol, price, reason)
            return

        order = {
            "order_id": f"part_{uuid.uuid4().hex[:8]}",
            "symbol": symbol,
            "action": "SELL",
            "quantity": exit_qty,
            "order_type": "MARKET",
            "strategy_id": "LIQUIDATION_PARTIAL",
            "execution_type": pos.get("execution_type", "Paper"),
            "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason
        }

        try:
            await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, order)
            logger.info(f"✅ PARTIAL EXIT ORDER sent: SELL {exit_qty} {symbol} @ {price:.2f} ({reason})")
            self.portfolio.update_position(symbol, {"quantity": qty - exit_qty})
            await self._redis.delete(f"Pending_Journal:{order['order_id']}")
            await self._redis.publish("CAPITAL_RELEASE", json.dumps({
                "symbol": symbol,
                "parent_uuid": pos.get("parent_uuid"),
                "released_qty": exit_qty,
                "reason": "TP1_PARTIAL_EXIT"
            }))
        except Exception as e:
            logger.error(f"Partial exit order failed: {e}")

    async def _attempt_exit(self, pos: dict, symbol: str, price: float, reason: str):
        """Fires exit order with HTTP 400 mitigation and sequenced basket exits."""
        current_qty = pos.get("quantity", 0)
        qty = abs(current_qty)
        if qty == 0:
            self.portfolio.remove_position(symbol)
            return

        action = "BUY" if current_qty < 0 else "SELL"

        order = {
            "order_id": str(uuid.uuid4()),
            "symbol": symbol,
            "action": action,
            "quantity": qty,
            "order_type": "MARKET",
            "strategy_id": "LIQUIDATION",
            "execution_type": pos.get("execution_type", "Paper"),
            "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason
        }

        # Wave 2: Strict Exit Sequencing (Shorts first)
        parent_uuid = pos.get("parent_uuid")
        if parent_uuid:
            basket_legs = self.portfolio.get_basket(parent_uuid)
            if len(basket_legs) > 1:
                logger.info(f"🔄 Sequenced liquidation for basket {parent_uuid} ({len(basket_legs)} legs)")
                basket_legs.sort(key=lambda x: 1 if x.get("quantity", 0) > 0 else 0)

                for b_pos in basket_legs:
                    b_symbol = b_pos["symbol"]
                    b_qty = abs(b_pos.get("quantity", 0))
                    if b_qty == 0:
                        continue

                    close_order = {
                        "order_id": str(uuid.uuid4()),
                        "symbol": b_symbol,
                        "action": "BUY" if b_pos.get("quantity", 0) < 0 else "SELL",
                        "quantity": b_qty,
                        "order_type": "MARKET",
                        "strategy_id": "LIQUIDATION_SEQUENCED",
                        "execution_type": b_pos.get("execution_type", "Paper"),
                        "price": price if b_symbol == symbol else 0,
                        "parent_uuid": parent_uuid,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "reason": f"{reason} | SEQUENCED"
                    }
                    await self._fire_order_with_retries(close_order, b_symbol, reason)
                    self.portfolio.remove_position(b_symbol)
                return

        # Single leg or fallback
        await self._fire_order_with_retries(order, symbol, reason)

    async def _fire_order_with_retries(self, order: dict, symbol: str, reason: str):
        """Encapsulated retry logic for order firing."""
        max_retries = 3
        price = order.get("price", 0)
        qty = order.get("quantity", 0)

        for attempt in range(1, max_retries + 1):
            try:
                await self.mq.send_json(self.order_pub, Topics.ORDER_INTENT, order)
                logger.info(f"✅ EXIT ORDER sent: {order['action']} {qty} {symbol} ({reason})")
                await self._redis.delete(f"Pending_Journal:{order['order_id']}")
                return
            except Exception as e:
                err_str = str(e).lower()
                http400_result = self._parse_http400_emsg(err_str)

                if http400_result == "circuit_limit":
                    logger.warning(f"HTTP 400 circuit limit. Recalculating. Attempt {attempt}.")
                    order["price"] = price * (0.98 if attempt % 2 == 0 else 1.02)
                    await asyncio.sleep(0.5 * attempt)
                elif http400_result == "abort":
                    logger.error(f"HTTP 400 ABORT: {err_str}")
                    asyncio.create_task(send_cloud_alert(
                        f"🆘 CRITICAL: EXIT ORDER ABORTED for {symbol}\n"
                        f"Error: {err_str[:200]}\nManual intervention required!",
                        alert_type="CRITICAL"
                    ))
                    return
                else:
                    logger.error(f"Exit order error (attempt {attempt}): {e}")
                    await asyncio.sleep(1)

        logger.error(f"❌ EXIT ORDER FAILED after {max_retries} attempts for {symbol}.")
        asyncio.create_task(send_cloud_alert(
            f"❌ EXIT ORDER EXHAUSTED: {symbol} | {reason}\nManual intervention required!",
            alert_type="ERROR"
        ))

    def _parse_http400_emsg(self, err_str: str) -> str:
        err_lower = err_str.lower()
        for pattern in CIRCUIT_LIMIT_STRINGS:
            if pattern in err_lower:
                return "circuit_limit"
        for pattern in ABORT_STRINGS:
            if pattern in err_lower:
                return "abort"
        return "retry"

    async def _record_barrier_exit(self, symbol: str, barrier: str, reason: str,
                                    exit_price: float, entry_price: float = 0.0):
        try:
            entry_f = float(entry_price) if entry_price else 0.0
            pnl_pts = float(exit_price - entry_f)
            record = json.dumps({
                "symbol": symbol,
                "barrier": barrier,
                "reason": reason,
                "exit_price": f"{exit_price:.2f}",
                "pnl_pts": f"{pnl_pts:.2f}",
                "ts": int(time.time()),
                "attribution": "LIQUIDATION_DAEMON"
            })
            await self._redis.lpush("barrier_exits", record)
            await self._redis.ltrim("barrier_exits", 0, 99)
        except Exception as e:
            logger.error(f"Barrier exit record error: {e}")

    async def _telegram_alert(self, message: str):
        asyncio.create_task(send_cloud_alert(message, alert_type="LIQUIDATION"))


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    daemon = LiquidationDaemon()
    asyncio.run(daemon.run())
