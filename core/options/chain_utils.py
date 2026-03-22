"""
Option Chain Utilities — Pure Strike Selection Math
Extracted from strategy_engine.py to eliminate 6× duplication of strike rounding.
"""

import logging
from core.greeks import BlackScholes

logger = logging.getLogger("ChainUtils")

# ── Index Metadata ────────────────────────────────────────────────────────────
# Moved here from strategy_engine.py global scope.

INDEX_METADATA = {
    "NIFTY50": {
        "increment": 50,
        "lot_size": 50,
        "expiry_day": "Thursday",
        "underlying": "NIFTY",
    },
    "BANKNIFTY": {
        "increment": 100,
        "lot_size": 15,
        "expiry_day": "Wednesday",
        "underlying": "BANKNIFTY",
    },
    "SENSEX": {
        "increment": 100,
        "lot_size": 10,
        "expiry_day": "Friday",
        "underlying": "SENSEX",
    },
}


def get_index_meta(symbol: str) -> dict:
    """Get index metadata for a symbol, defaulting to NIFTY50."""
    if symbol in INDEX_METADATA:
        return INDEX_METADATA[symbol]
    # Try to match by substring (e.g., "BANKNIFTY24..." → BANKNIFTY)
    for key in INDEX_METADATA:
        if key in symbol:
            return INDEX_METADATA[key]
    return INDEX_METADATA["NIFTY50"]


def get_increment(symbol: str) -> int:
    """Get strike increment for a symbol."""
    return get_index_meta(symbol)["increment"]


def get_lot_size(symbol: str) -> int:
    """Get lot size for a symbol."""
    return get_index_meta(symbol)["lot_size"]


# ── Strike Selection ──────────────────────────────────────────────────────────

def snap_strike(price: float, increment: int) -> float:
    """Round price to nearest valid strike. (was duplicated 6× in strategy_engine)"""
    return round(price / increment) * increment


def find_atm(price: float, symbol: str) -> float:
    """Find the ATM strike for a symbol at a given price."""
    return snap_strike(price, get_increment(symbol))


def find_strike_for_delta(
    target_delta: float,
    spot: float,
    T: float,
    r: float,
    sigma: float,
    otype: str,
    symbol: str = "NIFTY50",
    bs: BlackScholes | None = None,
) -> float:
    """Binary search to find the strike that yields the target delta.
    
    Extracted from IronCondorStrategy.find_strike_for_delta.
    Returns strike snapped to the asset's increment.
    """
    if bs is None:
        bs = BlackScholes()

    low, high = spot * 0.5, spot * 1.5
    for _ in range(15):
        mid = (low + high) / 2
        d = bs.delta(spot, mid, T, r, sigma, otype)
        if (otype == "call" and d > target_delta) or (otype == "put" and d < target_delta):
            low = mid if otype == "call" else low
            high = mid if otype == "put" else high
        else:
            high = mid if otype == "call" else high
            low = mid if otype == "put" else low

    return snap_strike(mid, get_increment(symbol))


# ── Multi-Leg Builders ────────────────────────────────────────────────────────

def build_iron_butterfly(
    spot: float,
    symbol: str,
    wing_offset_mult: int = 4,
) -> dict:
    """Build Iron Butterfly strikes (ATM body + OTM wings).
    
    Returns dict with keys: atm, call_wing, put_wing, wing_offset.
    Extracted from TastyTrade0DTEStrategy.on_tick.
    """
    inc = get_increment(symbol)
    atm = snap_strike(spot, inc)
    wing_offset = inc * wing_offset_mult

    return {
        "atm": atm,
        "call_wing": atm + wing_offset,
        "put_wing": atm - wing_offset,
        "wing_offset": wing_offset,
    }


def build_credit_spread(
    spot: float,
    side: str,
    symbol: str,
    offset: float = 100.0,
    width: float = 100.0,
) -> dict:
    """Build credit spread strikes (short + long protection).
    
    Args:
        side: "bull" (Bull Put Spread) or "bear" (Bear Call Spread)
    
    Returns dict with keys: short_strike, long_strike, otype.
    """
    inc = get_increment(symbol)
    if side == "bull":
        short = snap_strike(spot - offset, inc)
        long = short - width
        return {"short_strike": short, "long_strike": long, "otype": "PE"}
    else:
        short = snap_strike(spot + offset, inc)
        long = short + width
        return {"short_strike": short, "long_strike": long, "otype": "CE"}


def build_iron_condor(
    spot: float,
    symbol: str,
    T: float,
    r: float,
    sigma: float,
    wing_delta: float = 0.10,
    spread_delta: float = 0.15,
    bs: BlackScholes | None = None,
) -> dict:
    """Build Iron Condor strikes via delta targeting.
    
    Returns dict with keys: put_wing, put_main, call_main, call_wing.
    Extracted from IronCondorStrategy.on_tick.
    """
    return {
        "put_wing": find_strike_for_delta(-wing_delta, spot, T, r, sigma, "put", symbol, bs),
        "put_main": find_strike_for_delta(-spread_delta, spot, T, r, sigma, "put", symbol, bs),
        "call_main": find_strike_for_delta(spread_delta, spot, T, r, sigma, "call", symbol, bs),
        "call_wing": find_strike_for_delta(wing_delta, spot, T, r, sigma, "call", symbol, bs),
    }
