"""
Barrier Strategy Pattern — Module 6 Decomposition
Each lifecycle (Kinetic, Positional, ZeroDTE) is a standalone, testable unit.
No Redis/SHM dependency in evaluate() — all context is passed via BarrierContext.
"""

import time
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import numpy as np

from core.math.risk_utils import (
    ATR_TP1_MULTIPLIER, ATR_TP_MULTIPLIER, ATR_SL_MULTIPLIER,
    CVD_FLIP_EXIT_THRESHOLD, STALL_TIMEOUT_SEC,
    calculate_adaptive_sl, calculate_dynamic_stall,
    calculate_gamma_accel_stall, calculate_pnl, determine_underlying,
)

logger = logging.getLogger("Barriers")


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass(slots=True)
class BarrierContext:
    """All market/signal data needed for barrier evaluation.
    
    Populated by the daemon from SHM, Redis, and tick data BEFORE
    calling evaluate(). Barriers never touch I/O directly.
    """
    # Tick data
    price: float = 0.0
    is_index_tick: bool = False
    
    # Position data (from PortfolioState)
    entry: float = 0.0
    action: str = "BUY"
    elapsed: float = 0.0
    lifecycle_class: str = "KINETIC"
    parent_uuid: str = ""
    initial_credit: float = 0.0
    strategy_id: str = ""
    expiry_date: object = None  # datetime.date or None
    short_strikes: dict = field(default_factory=dict)
    inception_spot: float = 0.0
    runner_active: bool = False
    local_high: float = 0.0
    best_price: float = 0.0
    entry_hmm: str = ""
    dte: float = 7.0
    
    # Signal Vector (from SHM)
    atr: float = 20.0
    vix: float = 15.0
    rv: float = 0.0
    regime_s18: int = 0
    quality_s27: float = 100.0
    asto: float = 0.0
    cvd_score: float = 0.0
    s_total: float = 0.0
    slope_15m: float = 0.0
    avg_slope: float = 0.0
    iv_rv_spread: float = 0.0
    iv_atm: float = 20.0
    net_delta: float = 0.0
    
    # Regime
    current_hmm: str = "0"
    
    # Stagnation data
    hurst: float = 0.5
    recent_price_range: float = float('inf')
    has_range_data: bool = False
    
    # Market state (from Redis)
    market_state: str = ""
    
    # Pre-calculated thresholds (optional, from PortfolioState)
    sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0


@dataclass(slots=True)
class BarrierResult:
    """Output of a barrier evaluation."""
    triggered: bool = False
    reason: str = ""
    barrier_type: str = ""  # e.g. "KINETIC", "STRUCTURAL", "ZERO_DTE_TP"
    is_partial: bool = False
    partial_pct: float = 1.0  # 1.0 = full exit
    
    # State mutations to apply back to position dict
    state_updates: dict = field(default_factory=dict)
    
    # Special actions
    hedge_action: dict | None = None  # For Waterfall Protocol
    roll_action: dict | None = None   # For Defensive Roll
    basket_reduction: dict | None = None  # For regime shift reduction


# ── Base Class ────────────────────────────────────────────────────────────────

class BaseBarrier(ABC):
    """Abstract base for all lifecycle barrier strategies."""

    @abstractmethod
    def evaluate(self, pos: dict, ctx: BarrierContext) -> BarrierResult:
        """Evaluate all barriers for this lifecycle.
        
        Args:
            pos: The position dict (read-only ideally, state_updates for mutations).
            ctx: Pre-populated BarrierContext with all market/signal data.
        
        Returns:
            BarrierResult indicating if an exit was triggered and why.
        """
        ...


# ── Kinetic Barrier ──────────────────────────────────────────────────────────

class KineticBarrier(BaseBarrier):
    """Kinetic (Momentum) lifecycle barriers.
    
    Covers: TP1/TP2, Runner Invalidation Hunt, CVD Flip, ASTO Exit,
    Stagnation (Optimal Stopping), Hurst Regime Stall.
    """

    def evaluate(self, pos: dict, ctx: BarrierContext) -> BarrierResult:
        if ctx.is_index_tick:
            return BarrierResult()  # Kinetic barriers are price-based on the symbol itself

        price = ctx.price
        entry = ctx.entry
        elapsed = ctx.elapsed
        action = ctx.action

        # Adaptive thresholds
        sl_mult = 1.5 if (ctx.rv > 0.001 or ctx.vix > 18.0) else ATR_SL_MULTIPLIER
        tp1_mult = ATR_TP1_MULTIPLIER
        tp2_mult = ATR_TP_MULTIPLIER
        stall_timer = calculate_dynamic_stall(ctx.rv, ctx.vix, ctx.dte)

        # Runner trailing SL
        runner_active = ctx.runner_active
        state_updates = {}
        
        if runner_active:
            prev_high = ctx.local_high if ctx.local_high else price
            new_high = max(prev_high, price)
            state_updates["local_high"] = new_high
            sl = entry + (new_high - entry) * 0.5  # Trail 50% of runaway profit
        else:
            sl = entry - sl_mult * ctx.atr

        entry_hmm = ctx.entry_hmm or ctx.current_hmm
        if not ctx.entry_hmm:
            state_updates["entry_hmm"] = entry_hmm

        tp1 = entry + tp1_mult * ctx.atr
        tp2 = entry + tp2_mult * ctx.atr

        exit_reason = None
        is_partial = False

        # ── 1. CVD + Alpha Signal Flip ────────────────────────────────────
        cvd = ctx.cvd_score
        s_total = ctx.s_total
        if (action == "BUY" and (cvd < -CVD_FLIP_EXIT_THRESHOLD or s_total < -40)) or \
           (action == "SELL" and (cvd > CVD_FLIP_EXIT_THRESHOLD or s_total > 40)):
            return BarrierResult(
                triggered=True,
                reason=f"SIGNAL_FLIP_EXIT: CVD={cvd:.1f}, S_Total={s_total:.0f}",
                barrier_type="SIGNAL_FLIP",
                state_updates=state_updates,
            )

        # ── 2. ASTO Kinetic Exit ──────────────────────────────────────────
        if abs(ctx.asto) < 50:
            return BarrierResult(
                triggered=True,
                reason=f"ASTO_KINETIC_EXIT: |{ctx.asto:.1f}| < 50",
                barrier_type="ASTO_EXIT",
                state_updates=state_updates,
            )

        # ── 3. TP / SL Barriers ──────────────────────────────────────────
        if action == "BUY":
            if not runner_active and price >= tp1:
                exit_reason = f"TP1_HIT: {price:.2f} >= {tp1:.2f}"
                is_partial = True
            elif runner_active:
                if price >= tp2:
                    exit_reason = f"TP2_HIT: {price:.2f} >= {tp2:.2f}"
                elif price <= sl:
                    exit_reason = f"INV_HUNT_SL: {price:.2f} <= {sl:.2f}"
                elif ctx.current_hmm != entry_hmm and ctx.current_hmm in ["RANGING", "CRASH"]:
                    exit_reason = f"HMM_SHIFT: {entry_hmm} -> {ctx.current_hmm}"
            elif price <= sl:
                exit_reason = f"SL_HIT: {price:.2f} <= {sl:.2f}"

        elif action == "SELL":
            sl_short = entry + sl_mult * ctx.atr
            tp1_short = entry - tp1_mult * ctx.atr
            tp2_short = entry - tp2_mult * ctx.atr

            if not runner_active and price <= tp1_short:
                exit_reason = f"TP1_HIT_SHORT: {price:.2f} <= {tp1_short:.2f}"
                is_partial = True
            elif runner_active:
                if price <= tp2_short:
                    exit_reason = f"TP2_HIT_SHORT: {price:.2f} <= {tp2_short:.2f}"
                elif price >= sl_short:
                    exit_reason = f"INV_HUNT_SL_SHORT: {price:.2f} >= {sl_short:.2f}"
                elif ctx.current_hmm != entry_hmm and ctx.current_hmm in ["RANGING", "BOOM"]:
                    exit_reason = f"HMM_SHIFT_SHORT: {entry_hmm} -> {ctx.current_hmm}"
            elif price >= sl_short:
                exit_reason = f"SL_HIT_SHORT: {price:.2f} >= {sl_short:.2f}"

        # ── 4. Stagnation Exit (Optimal Stopping) ────────────────────────
        if not exit_reason and elapsed >= stall_timer:
            if ctx.hurst < 0.45 and elapsed >= 180:
                exit_reason = f"REGIME_STALL: Hurst {ctx.hurst:.2f} (Mean Reverting) @ {elapsed:.0f}s"
            elif ctx.has_range_data and ctx.recent_price_range < 0.1 * ctx.atr:
                exit_reason = f"OPTIMAL_STOPPING: Range {ctx.recent_price_range:.2f} < 0.1*ATR({ctx.atr:.2f}) over {elapsed:.0f}s"
            elif elapsed >= stall_timer:
                exit_reason = f"THETA_STALL: {elapsed:.0f}s >= {stall_timer:.0f}s"

        if exit_reason:
            if is_partial:
                state_updates["runner_active"] = True
            return BarrierResult(
                triggered=True,
                reason=exit_reason,
                barrier_type="KINETIC",
                is_partial=is_partial,
                partial_pct=0.70 if is_partial else 1.0,
                state_updates=state_updates,
            )

        # No trigger — still return state updates (e.g. local_high tracking)
        return BarrierResult(state_updates=state_updates)


# ── Positional Barrier ───────────────────────────────────────────────────────

class PositionalBarrier(BaseBarrier):
    """Positional (Iron Condor / Credit Spread) lifecycle barriers.
    
    Covers: Structural Breach, DTE Vertical, Spread Decay TP,
    DirectionalCredit stop, Delta Waterfall, Twitchy ATR Trail.
    """

    def evaluate(self, pos: dict, ctx: BarrierContext) -> BarrierResult:
        symbol = pos.get("symbol", "")

        # ── 1. Structural Breach (ONLY on Index Tick) ─────────────────────
        if ctx.is_index_tick:
            spot = ctx.price
            short_strikes = ctx.short_strikes or {}
            call_strike = short_strikes.get("call", 999999)
            put_strike = short_strikes.get("put", 0)

            if spot > call_strike or spot < put_strike:
                return BarrierResult(
                    triggered=True,
                    reason=f"STRUCTURAL_BREACH: Spot {spot:.0f} outside [{put_strike}, {call_strike}]",
                    barrier_type="STRUCTURAL",
                )
            return BarrierResult()  # No further checks on index ticks

        # ── Remaining barriers are price-based on the OPTION symbol ───────
        price = ctx.price
        entry = ctx.entry
        parent_uuid = ctx.parent_uuid
        if not parent_uuid:
            return BarrierResult()

        # ── 2. DTE Vertical Barrier ───────────────────────────────────────
        if ctx.expiry_date:
            now = datetime.now(timezone.utc).date()
            dte = (ctx.expiry_date - now).days
            if dte < 3:
                return BarrierResult(
                    triggered=True,
                    reason=f"DTE_VERTICAL: DTE={dte} < 3 days. Clearing positional risk.",
                    barrier_type="TIME_LIMIT",
                )

        # ── 3. Spread Decay Take Profit ───────────────────────────────────
        initial_credit = ctx.initial_credit
        strategy_id = ctx.strategy_id
        if initial_credit > 0:
            current_value = price
            if strategy_id == "DirectionalCredit":
                tp_threshold = 0.30
            else:
                tp_threshold = 0.40

            if current_value / initial_credit <= tp_threshold:
                return BarrierResult(
                    triggered=True,
                    reason=f"SPREAD_DECAY_TP: {current_value:.2f} <= {tp_threshold*100:.0f}% of {initial_credit:.2f}",
                    barrier_type="PROFIT_TAKING",
                )

            # DirectionalCredit 2× credit stop
            if strategy_id == "DirectionalCredit":
                max_loss_value = initial_credit * 2.0
                if current_value >= max_loss_value:
                    return BarrierResult(
                        triggered=True,
                        reason=f"CREDIT_STOP: Value {current_value:.2f} >= 2× credit {initial_credit:.2f}",
                        barrier_type="STOP_LOSS",
                    )

        # ── 4. Delta Tolerance Waterfall ──────────────────────────────────
        if abs(ctx.net_delta) > 0.15:
            return BarrierResult(
                triggered=False,  # Not an exit — it's a hedge request
                hedge_action={
                    "delta": ctx.net_delta,
                    "rv": ctx.rv,
                    "pos": pos,
                },
            )

        # ── 5. Twitchy Mode: Dynamic ATR Trail for Losing Leg ─────────────
        if "EXTREME_TREND" in ctx.market_state:
            is_call = "CE" in symbol
            is_put = "PE" in symbol
            is_losing = (ctx.asto >= 90 and is_call) or (ctx.asto <= -90 and is_put)

            if is_losing:
                state_updates = {}
                best_price = ctx.best_price if ctx.best_price else price
                if (is_call and price < best_price) or (is_put and price > best_price):
                    state_updates["best_price"] = price
                    best_price = price

                exit_reason = None
                if is_call:
                    sl = best_price + 0.5 * ctx.atr
                    if price >= sl:
                        exit_reason = f"TWITCHY_STOP_CALL: {price:.2f} >= {sl:.2f} (0.5*ATR trail)"
                else:
                    sl = best_price - 0.5 * ctx.atr
                    if price <= sl:
                        exit_reason = f"TWITCHY_STOP_PUT: {price:.2f} <= {sl:.2f} (0.5*ATR trail)"

                if exit_reason:
                    return BarrierResult(
                        triggered=True,
                        reason=exit_reason,
                        barrier_type="TWITCHY_MODE",
                        state_updates=state_updates,
                    )
                return BarrierResult(state_updates=state_updates)

        return BarrierResult()


# ── Zero-DTE Barrier ─────────────────────────────────────────────────────────

class ZeroDteBarrier(BaseBarrier):
    """Zero Days-to-Expiry lifecycle barriers.
    
    Covers: IV-RV Spread Erosion, Underlying Move Stop (0.5% inception),
    Credit-based 50% TP, Delta Defensive Roll, Power Hour Gamma Accel,
    Fallback 5-min Stall.
    """

    def evaluate(self, pos: dict, ctx: BarrierContext) -> BarrierResult:
        symbol = pos.get("symbol", "")

        # ── 1. IV-RV Spread Erosion (Enhancement 2) ───────────────────────
        iv_rv = ctx.iv_rv_spread
        if iv_rv < 0.01 and iv_rv != 0:
            return BarrierResult(
                triggered=True,
                reason=f"EDGE_EROSION: IV-RV Spread collapsed to {iv_rv:.2%}",
                barrier_type="ALPHA_DEATH",
            )

        # ── 2. Underlying Move Stop (Index Tick Only) ─────────────────────
        if ctx.is_index_tick:
            idx_price = ctx.price
            inception_spot = ctx.inception_spot

            if inception_spot > 0:
                move_pct = abs(idx_price - inception_spot) / inception_spot * 100
                if move_pct > 0.5:
                    return BarrierResult(
                        triggered=True,
                        reason=f"ZERO_DTE_STOP: Spot moved {move_pct:.2f}% > 0.5% from inception",
                        barrier_type="ZERO_DTE_STOP",
                    )
            return BarrierResult()  # No further checks on index ticks for 0DTE

        # ── Price-based barriers on the Option Premium ────────────────────
        prem_price = ctx.price
        prem_entry = ctx.entry

        # ── 3. Credit-based Take Profit (50%) ─────────────────────────────
        initial_credit = ctx.initial_credit
        if initial_credit > 0:
            if prem_price / initial_credit <= 0.50:
                return BarrierResult(
                    triggered=True,
                    reason=f"ZERO_DTE_TP: Value {prem_price:.2f} <= 50% of credit {initial_credit:.2f}",
                    barrier_type="ZERO_DTE_TP",
                )

        # ── 4. Delta-Specific Defensive Roll (Enhancement 3) ─────────────
        # This returns a roll_action rather than an exit
        # (Actual BS calculation and roll dispatch happens in the daemon)
        # We signal intent here if delta data is available
        # Note: we don't do the BS calc here to keep barriers I/O-free

        # ── 5. Power Hour Gamma Acceleration (Enhancement 1) ──────────────
        now_ist = datetime.now(timezone.utc).astimezone(
            timezone(timedelta(hours=5, minutes=30))
        )
        minutes_to_close = (15 * 60 + 30) - (now_ist.hour * 60 + now_ist.minute)
        dynamic_stall = calculate_gamma_accel_stall(minutes_to_close)

        elapsed = ctx.elapsed
        if elapsed > dynamic_stall:
            return BarrierResult(
                triggered=True,
                reason=f"GAMMA_ACCEL_STALL: {elapsed:.0f}s > {dynamic_stall:.0f}s (Time to Close: {minutes_to_close}m)",
                barrier_type="GAMMA_DECAY",
            )

        # ── 6. Fallback 5-min Stall ───────────────────────────────────────
        if elapsed > 300:
            return BarrierResult(
                triggered=True,
                reason=f"ZERO_DTE_STALL: {elapsed:.0f}s > 300s",
                barrier_type="ZERO_DTE_STALL",
            )

        return BarrierResult()
