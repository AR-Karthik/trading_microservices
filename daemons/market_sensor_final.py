"""
Market Sensor Daemon
High-performance asynchronous data ingestion and signal calculation engine.
Ingests real-time tick sequences, dispatches intense mathematical computations (Greeks, GEX) 
to dedicated CPU sub-processes, and merges quantitative arrays to form unified Alpha Scores.
"""

import asyncio
import collections
import json
import logging
import math
import multiprocessing as mp
from queue import Empty
import sys
import time
import queue
import re
from datetime import datetime, timezone
from typing import Any  # [R2-20] Removed duplicate 'import sys'

import numpy as np # type: ignore
import polars as pl # type: ignore
import redis.asyncio as redis # type: ignore
import zmq
import zmq.asyncio

try:
    import uvloop
except ImportError:
    uvloop = None

import os
from core.logger import setup_logger # type: ignore
from core.shm import ShmManager, SignalVector # type: ignore
from core.greeks import BlackScholes # type: ignore
from core.mq import MQManager, Ports, Topics, NumpyEncoder # type: ignore
from core.alerts import send_cloud_alert # type: ignore
from core.health import HeartbeatProvider # type: ignore

# Try to import Rust extension for high-performance math
try:
    import tick_engine # type: ignore
    HAS_RUST_ENGINE = True
except ImportError:
    HAS_RUST_ENGINE = False

logger = setup_logger("MarketSensor", log_file="logs/market_sensor.log")

# ── Constants ────────────────────────────────────────────────────────────────

TOP_10_HEAVYWEIGHTS = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", 
    "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT"
]

# Instrument constituent weight definitions locking alpha generation to core liquidity
INDEX_COMPONENTS = {
    "NIFTY50": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT"],
    "BANKNIFTY": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "AUBL", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK"],
    "SENSEX": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "TCS", "LT", "AXISBANK", "SBIN", "KOTAKBANK"]
}
OFI_WINDOW = 100          # ticks for rolling OFI
DISPERSION_WINDOW_MIN = 3 # minutes for correlation rolling window
# Risk-free rate continuously resolved from macroeconomic models
NEAR_TERM_DTE = 2         # days for "near term" IV
FAR_TERM_DTE = 30         # days for "far term" IV

# Signal Math Helpers

def find_zero_gamma_level(prices_arr: np.ndarray, current_spot: float) -> float:
    """
    Estimates the Zero Gamma Level (ZGL) where dealer hedging flips from short to long gamma.
    Note: A true ZGL requires dealer positioning data. This is a heuristic based on
    volume-weighted clusters (Volume Nodes) which often act as gamma pin levels.
    """
    if len(prices_arr) == 0:
        return current_spot
    # Heuristic: Weighted average of the last 100 prices (simulating liquidity clustering)
    if len(prices_arr) >= 100:
        return float(np.mean(prices_arr[-100:]))
    return float(np.mean(prices_arr))

def calculate_kaufman_er(series: np.ndarray, window: int = 10) -> float:
    """
    Kaufman Efficiency Ratio: Net Change / Sum of Absolute Changes.
    Requires window + 1 points to get 'window' returns.
    """
    if len(series) < window + 1:
        # print(f"DEBUG: len(series)={len(series)} window={window}")
        return 0.5
    net_change = abs(series[-1] - series[-(window+1)])
    # Sum of absolute differences over the window
    sum_abs_changes = np.sum(np.abs(np.diff(series[-(window+1):])))
    return float(net_change / sum_abs_changes) if sum_abs_changes > 0 else 0.0

def calculate_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 14) -> float:
    """Simplified ADX calculation for trend strength. Returns float [0-100]."""
    if len(close) < window * 2:
        return 20.0
    
    up_move = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    tr = np.maximum(high[1:] - low[1:], 
                    np.maximum(np.abs(high[1:] - close[:-1]), 
                               np.abs(low[1:] - close[:-1])))
    
    # Simple moving average for smoothing in this high-speed context
    tr_smooth = np.mean(tr[-window:])
    if tr_smooth <= 0: return 20.0
    
    plus_di = 100 * (np.mean(plus_dm[-window:]) / tr_smooth)
    minus_di = 100 * (np.mean(minus_dm[-window:]) / tr_smooth)
    
    div = plus_di + minus_di
    dx = 100 * (np.abs(plus_di - minus_di) / div) if div > 0 else 0
    return float(dx)




def calculate_hurst(series: np.ndarray) -> float:
    """Hurst exponent helper."""
    if len(series) < 50:
        return 0.5
    lags = range(2, 20)
    tau = [np.sqrt(np.std(np.subtract(series[lag:], series[:-lag]))) for lag in lags]
    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    return float(poly[0] * 2.0)

class AdaptiveSuperTrendOscillator:
    """
    Adaptive SuperTrend Oscillator (ASTO) - S23
    Calculates dynamic HL2-based bands with adaptive ATR-10 multipliers.
    Normalizes price position to [-100, +100] for dual-identity logic.
    """
    def __init__(self, base_multiplier: float = 3.0, atr_period: int = 10, sensitivity: float = 0.5):
        self.base_multiplier = base_multiplier
        self.phi = sensitivity
        self.atr_buffer = collections.deque(maxlen=atr_period)
        self.last_upper = 0.0
        self.last_lower = 0.0
        self.prev_close = 0.0

    def compute(self, high: float, low: float, close: float, z_vol: float) -> tuple[float, int, float]:
        """
        Refined Core Logic:
        1. Median Pivot: HL2
        2. Multiplier: Base + (Z_vol * 0.5)
        3. Normalization: Map price within bands to -100 to +100
        """
        # 1. HL2 Median Pivot
        hl2 = (high + low) / 2.0
        
        # 2. Dynamic TR/ATR
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close)) if self.prev_close > 0 else (high - low)
        self.atr_buffer.append(tr)
        self.prev_close = close
        atr = np.mean(self.atr_buffer) if self.atr_buffer else 1.0
        
        # 3. Adaptive Multiplier (Expansion/Compression)
        m_adaptive = self.base_multiplier + (z_vol * self.phi)
        
        # 4. Dynamic Bands
        upper_band = hl2 + (m_adaptive * atr)
        lower_band = hl2 - (m_adaptive * atr)
        
        # SuperTrend Persistence
        if self.last_upper > 0:
            if close < self.last_upper: upper_band = min(upper_band, self.last_upper)
            if close > self.last_lower: lower_band = max(lower_band, self.last_lower)
            
        self.last_upper = upper_band
        self.last_lower = lower_band
        
        # 5. Normalization [-100, +100]
        band_range = upper_band - lower_band
        if band_range > 0:
            # Normalized map: upper=+100, lower=-100, hl2=0
            asto = ((close - hl2) / (band_range / 2.0)) * 100.0
            asto = max(-100.0, min(100.0, asto))
        else:
            asto = 0.0
            
        # Regime Detection (|ASTO| > 70 = Trend)
        regime = 1 if abs(asto) > 70 else 0
        
        return float(asto), int(regime), float(m_adaptive)


def _compute_worker(in_queue: mp.Queue, out_queue: mp.Queue):
    """
    Isolated OS process — runs all heavy math.
    Reads feature snapshots from in_queue, pushes computed signals to out_queue.
    GIL is completely bypassed since this runs in a separate Python interpreter.
    """
    import numpy as np
    from scipy.optimize import brentq  # type: ignore
    from scipy.stats import norm       # type: ignore
    from core.greeks import BlackScholes
    import collections

    # Per-symbol state for ASTO and RSI
    asto_engines = collections.defaultdict(lambda: AdaptiveSuperTrendOscillator())
    atr_histories = collections.defaultdict(lambda: collections.deque(maxlen=100))
    rsi_windows = collections.defaultdict(lambda: collections.deque(maxlen=15)) # 14 + current

    def bs_call_price(S, K, T, r, sigma):
        return BlackScholes.call_price(S, K, T, r, sigma)

    def bs_gamma(S, K, T, r, sigma):
        return BlackScholes.gamma(S, K, T, r, sigma)

    def bs_delta(S, K, T, r, sigma, opt_type="call"):
        return BlackScholes.delta(S, K, T, r, sigma, opt_type)

    def bs_vanna(S, K, T, r, sigma):
        # Vanna = dGamma/dVol or dDelta/dVol
        # Simplified proxy for retail-heavy flows
        return BlackScholes.gamma(S, K, T, r, sigma) * (S / sigma) * 0.01

    def bs_charm(S, K, T, r, sigma, opt_type="call"):
        # Charm = Delta decay vs Time
        return BlackScholes.theta(S, K, T, r, sigma, opt_type) * 0.1

    def compute_signals(snapshot: dict) -> dict:
        """Main compute function called for each tick snapshot."""
        result = {}

        # ── Log-OFI Z-score ────────────────────────────────────────────────
        ofi_series = np.array(snapshot.get("ofi_series", []))
        if len(ofi_series) >= 20:
            log_ofi = np.log1p(np.abs(ofi_series)) * np.sign(ofi_series)
            mu, sigma_ofi = np.mean(log_ofi), np.std(log_ofi)
            result["log_ofi_zscore"] = float((log_ofi[-1] - mu) / sigma_ofi) if sigma_ofi > 0 else 0.0
        else:
            result["log_ofi_zscore"] = 0.0

        # ── Index Dispersion (Pearson correlation matrix) ──────────────────
        hw_prices: dict[str, list] = snapshot.get("hw_prices", {})
        if len(hw_prices) >= 2:
            corr_coeffs = []
            hw_keys = list(hw_prices.keys())
            for i in range(len(hw_keys)):
                for j in range(i + 1, len(hw_keys)):
                    a = np.array(hw_prices[hw_keys[i]])
                    b = np.array(hw_prices[hw_keys[j]])
                    min_len = min(len(a), len(b))
                    if min_len >= 10:
                        corr_coeffs.append(np.corrcoef(a[-min_len:], b[-min_len:])[0, 1])
            result["dispersion_coeff"] = float(np.mean(corr_coeffs)) if corr_coeffs else 0.5
        else:
            result["dispersion_coeff"] = 0.5

        # ── Volatility Term Structure ──────────────────────────────────────
        near_iv = snapshot.get("near_term_iv", 0.18)
        far_iv = snapshot.get("far_term_iv", 0.16)
        result["vol_term_ratio"] = float(near_iv / far_iv) if far_iv > 0 else 1.0

        # ── Zero Gamma Level & GEX ───────────────────────────────────────────
        spot = snapshot.get("spot", 22000.0)
        strikes = snapshot.get("strikes", [])
        T = snapshot.get("dte", 2) / 365.0
        iv = snapshot.get("atm_iv", 0.18)
        symbol = snapshot.get("symbol", "NIFTY50")

        # [Audit 2.3] Option Liquidity Guard
        has_liquid_options = (len(strikes) >= 5 and 0.05 < iv < 1.5 and T > 0)
        
        if has_liquid_options:
            r = float(snapshot.get("risk_free_rate", 0.065))
            result["zero_gamma_level"] = find_zero_gamma_level(np.array(snapshot.get("price_series", [spot])), spot)
            gex_vals = [bs_gamma(spot, K, T, r, iv) * (1 if K >= spot else -1) for K in strikes]
            result["gex_sign"] = "NEGATIVE" if sum(gex_vals) < 0 else "POSITIVE"
            
            K_atm = min(strikes, key=lambda k: abs(k - spot))
            result["charm"] = float(bs_charm(spot, K_atm, T, r, iv, "call"))
            result["vanna"] = float(bs_vanna(spot, K_atm, T, r, iv))
            result["toxic_option"] = bool(result["charm"] < -0.05)
        else:
            result["zero_gamma_level"] = spot
            result["gex_sign"] = "NEUTRAL"
            result["charm"] = 0.0
            result["vanna"] = 0.0
            result["toxic_option"] = False
