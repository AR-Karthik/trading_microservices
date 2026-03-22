"""
Pure Risk Math Functions — Module 6 Decomposition
Extracted from LiquidationDaemon to enable reuse across Strategy Engine and Guardian.
All functions are stateless (no Redis, no SHM, no I/O).
"""

import numpy as np

# ── Constants (Moved from LiquidationDaemon) ─────────────────────────────────

ATR_TP1_MULTIPLIER = 1.2
ATR_TP_MULTIPLIER = 2.5
ATR_SL_MULTIPLIER = 1.0
STALL_TIMEOUT_SEC = 300

KINETIC_SL_MULT_BASE = 1.0
POSITIONAL_SL_MULT_BASE = 2.0
CVD_FLIP_EXIT_THRESHOLD = 5
BARRIER2_SPREAD_CROSS_PCT = 0.10

SLIPPAGE_BUDGET_BASE = 0.02
SLIPPAGE_HALT_TTL_SEC = 60


def determine_underlying(symbol: str) -> str:
    """Extract the underlying index from an option/future symbol.
    
    >>> determine_underlying("BANKNIFTY 26MAR 48200 CE")
    'BANKNIFTY'
    >>> determine_underlying("NIFTY 26MAR 22500 PE")
    'NIFTY50'
    >>> determine_underlying("SENSEX 26MAR 72000 CE")
    'SENSEX'
    """
    if "BANKNIFTY" in symbol:
        return "BANKNIFTY"
    elif "SENSEX" in symbol:
        return "SENSEX"
    elif "NIFTY" in symbol:
        return "NIFTY50"
    return "NIFTY50"  # Default fallback


def calculate_pnl(price: float, entry: float, action: str) -> float:
    """Unified P&L calculation for any position.
    
    Args:
        price: Current market price.
        entry: Entry price.
        action: 'BUY' or 'SELL'.
    
    Returns:
        P&L in price points (positive = profit).
    """
    if action == "BUY":
        return price - entry
    else:  # SELL
        return entry - price


def calculate_adaptive_sl(rv: float, vix: float, atr: float,
                          base_mult: float = ATR_SL_MULTIPLIER) -> float:
    """Adaptive Stop-Loss scaling based on market volatility.
    
    Widens the SL multiplier from base to 1.5× when RV > 0.001 or VIX > 18.
    
    Args:
        rv: Realized volatility.
        vix: India VIX value.
        atr: Average True Range.
        base_mult: Base SL multiplier (default: 1.0 for Kinetic).
    
    Returns:
        Absolute SL distance in price points.
    """
    sl_mult = 1.5 if (rv > 0.001 or vix > 18.0) else base_mult
    return sl_mult * atr


def calculate_dynamic_stall(rv: float, vix: float, dte: float = 7.0) -> float:
    """Theta-Aware Dynamic Stall Timer (Phase 15.2).
    
    Shortens the stall timer in low-vol environments and scales
    aggressively for 0DTE positions.
    
    Args:
        rv: Realized volatility.
        vix: India VIX value.
        dte: Days to expiry.
    
    Returns:
        Stall timeout in seconds.
    """
    base_stall = 180 if (rv < 0.0005 or vix < 12.0) else STALL_TIMEOUT_SEC
    theta_scaling = 1.0 - (0.5 if dte < 1.0 else 0.0)
    return float(base_stall * theta_scaling)


def check_velocity_breach(slope_1m: float, avg_slope: float, pnl: float) -> bool:
    """Velocity-based Stop-Loss Lead-Indicator (Enhancement 4).
    
    Triggers if current velocity is 2× the 15m average AND the velocity
    direction is AGAINST the position's P&L.
    
    Args:
        slope_1m: Current 1-minute slope (proxy from 15m ÷ 15).
        avg_slope: Average slope over the 15m window.
        pnl: Current position P&L (positive = profit).
    
    Returns:
        True if velocity breach is detected.
    """
    if abs(avg_slope) == 0:
        return False
    return abs(slope_1m) > (abs(avg_slope) * 2.0) and (np.sign(slope_1m) != np.sign(pnl))


def calculate_gamma_accel_stall(minutes_to_close: int) -> float:
    """Power Hour Gamma Acceleration (Enhancement 1).
    
    Shrinks the stall timer as market close approaches.
    At 14:00 (90 min to close): full timer.
    At 15:00 (30 min to close): ~100s.
    At 15:15 (15 min to close): ~50s.
    
    Args:
        minutes_to_close: Minutes until 15:30 IST market close.
    
    Returns:
        Dynamic stall timer in seconds.
    """
    time_mult = max(0.1, min(1.0, minutes_to_close / 90.0))
    return 300.0 * time_mult


def calculate_dynamic_slippage_budget(rv: float) -> float:
    """Dynamic Slippage Adjustment (Phase 15.1).
    
    Args:
        rv: Realized volatility.
    
    Returns:
        Slippage budget as a fraction (e.g., 0.02 = 2%).
    """
    return SLIPPAGE_BUDGET_BASE * (1.5 if rv > 0.001 else 1.0)


def pre_calculate_thresholds(entry: float, atr: float, action: str,
                             rv: float = 0.0, vix: float = 15.0) -> dict:
    """Pre-calculate hard price triggers for a position.
    
    Should be called once when a position is armed/updated. The tick loop
    then becomes a simple `if price <= trigger` comparison.
    
    Args:
        entry: Entry price.
        atr: Average True Range.
        action: 'BUY' or 'SELL'.
        rv: Realized volatility (for adaptive SL).
        vix: India VIX value (for adaptive SL).
    
    Returns:
        Dict with 'sl_price', 'tp1_price', 'tp2_price'.
    """
    sl_mult = 1.5 if (rv > 0.001 or vix > 18.0) else ATR_SL_MULTIPLIER

    if action == "BUY":
        return {
            "sl_price": entry - sl_mult * atr,
            "tp1_price": entry + ATR_TP1_MULTIPLIER * atr,
            "tp2_price": entry + ATR_TP_MULTIPLIER * atr,
        }
    else:  # SELL
        return {
            "sl_price": entry + sl_mult * atr,
            "tp1_price": entry - ATR_TP1_MULTIPLIER * atr,
            "tp2_price": entry - ATR_TP_MULTIPLIER * atr,
        }
