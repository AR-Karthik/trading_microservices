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

def calculate_smart_flow(high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray) -> float:
    """Composite wave of Chaikin Money Flow (CMF) and MFI. Returns [-100, 100]."""
    if len(close) < 20: return 0.0
    # 1. CMF (20-period)
    hl = high - low
    mfv = np.where(hl > 0, volume * ((close - low) - (high - close)) / hl, 0.0)
    cmf = np.sum(mfv[-20:]) / np.sum(volume[-20:]) if np.sum(volume[-20:]) > 0 else 0.0
    # 2. MFI (14-period)
    tp = (high + low + close) / 3.0
    rmf = tp * volume
    pos_mf = np.sum(np.where(tp[1:] > tp[:-1], rmf[1:], 0.0)[-14:])
    neg_mf = np.sum(np.where(tp[1:] < tp[:-1], rmf[1:], 0.0)[-14:])
    mfi = 100 - (100 / (1 + (pos_mf / neg_mf))) if neg_mf > 0 else 50.0
    # Composite Result
    return float(((cmf * 100.0) + ((mfi - 50.0) * 2.0)) / 2.0)




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

        # ── Volatility Vector (RV, IV-RV, Percentile) ──────────────────────
        near_iv = float(snapshot.get("near_term_iv", 0.18))
        far_iv = float(snapshot.get("far_term_iv", 0.16))
        atm_iv = float(snapshot.get("atm_iv", 0.18))
        result["vol_term_ratio"] = float(near_iv / far_iv) if far_iv > 0 else 1.0
        
        # [Audit] 10-day RV Annualized logic
        prices = np.array(snapshot.get("price_series", [snapshot.get("spot", 0)]))
        if len(prices) >= 10:
            returns = np.diff(np.log(prices))
            # Standardize to annual vol (252 days * 6.25 trading hours * 60 mins)
            rv_10d = np.std(returns) * np.sqrt(252 * 375 * 60) # 375 mins per day
            result["rv_10d"] = float(rv_10d)
            # [Audit-Fix] Spread uses the stabilized IV from the same compute cycle
            result["iv_rv_spread"] = float(atm_iv - rv_10d)
        else:
            result["rv_10d"] = atm_iv # Fallback
            result["iv_rv_spread"] = 0.0

        # IV Percentile rank vs 14-day history
        iv_hist = snapshot.get("iv_history", [atm_iv])
        
        # [Audit-Fix] Hybrid IV Stabilization (S12)
        # If in simulation mode, adjust API IV to track price moves to maintain edge parity
        off_hour_sim = snapshot.get("off_hour_sim", False)
        if off_hour_sim:
            snap_p = snapshot.get("sim_snap_price", spot)
            # Simple inverse skew proxy: IV expands 0.1% for every 1% price drop
            price_ratio = (spot / snap_p) if snap_p > 0 else 1.0
            atm_iv = atm_iv * (1.0 + (1.0 - price_ratio) * 0.1) 
            result["atm_iv_sim"] = float(atm_iv)

        if len(iv_hist) >= 2:
            iv_min, iv_max = min(iv_hist), max(iv_hist)
            result["iv_percentile"] = float((atm_iv - iv_min) / (iv_max - iv_min) * 100) if iv_max > iv_min else 50.0
        else:
            result["iv_percentile"] = 50.0

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

        # ── Price Vector (Z-Scores) ────────────────────────────────────────
        prices = np.array(snapshot.get("price_series", [spot]))
        atr = result.get("atr", 20.0)
        if len(prices) >= 20: # Use rolling window instead of full day to capture 'current' trend
            day_high = np.max(prices)
            day_low = np.min(prices)
            result["high_z"] = float((spot - day_high) / atr) if atr > 0 else 0.0
            result["low_z"] = float((spot - day_low) / atr) if atr > 0 else 0.0
        else:
            result["high_z"] = 0.0
            result["low_z"] = 0.0

        # ── CVD Absorption ────────────────────────────────────────────────
        cvd_series = np.array(snapshot.get("cvd_series", [0.0]))
        price_series = np.array(snapshot.get("price_series", [spot]))
        if len(cvd_series) >= 5 and len(price_series) >= 5:
            price_ll = price_series[-1] < price_series[-2]          # lower low price
            cvd_hl = cvd_series[-1] > cvd_series[-2]                # higher low CVD
            result["cvd_absorption"] = bool(price_ll and cvd_hl)    # bullish absorption
            # Count consecutive CVD flips (for barrier 3 in liquidation)
            flips = sum(1 for i in range(1, min(10, len(cvd_series)))
                        if np.sign(cvd_series[-i]) != np.sign(cvd_series[-i-1]))
            result["cvd_flip_ticks"] = int(flips)
        else:
            result["cvd_absorption"] = False
            result["cvd_flip_ticks"] = 0

        # ── Futures Basis Dislocation ──────────────────────────────────────
        basis_series = np.array(snapshot.get("basis_series", [0.0]))
        if len(basis_series) >= 20:
            mu_b, s_b = np.mean(basis_series), np.std(basis_series)
            z = (basis_series[-1] - mu_b) / s_b if s_b > 0 else 0.0
            result["basis_zscore"] = float(z)
            result["price_dislocation"] = bool(abs(z) > 3.0)
        else:
            result["basis_zscore"] = 0.0
            result["price_dislocation"] = False

        # ── Spot Z-score vs 15-min mean ────────────────────────────────────
        spot_15m = np.array(snapshot.get("spot_15m_series", [spot]))
        if len(spot_15m) >= 20:
            mu_s, s_s = np.mean(spot_15m), np.std(spot_15m)
            result["spot_zscore_15m"] = float((spot - mu_s) / s_s) if s_s > 0 else 0.0
        else:
            result["spot_zscore_15m"] = 0.0

        # ── VPIN (Flow Toxicity) ───────────────────────────────────────────
        vpin_series = snapshot.get("vpin_series", [0.0])
        result["vpin"] = float(vpin_series[-1]) if vpin_series else 0.0
        # Veto long trades if VPIN > 0.8
        result["flow_toxicity_veto"] = bool(result["vpin"] > 0.8)

        # ── v6.5 Deterministic Guardrails ──────────────────────────────────
        prices = np.array(snapshot.get("price_series", [spot]))
        result["hurst"] = snapshot.get("hurst_val", 0.5) # Hurst is slow, calculated in main
        result["kaufman_er"] = calculate_kaufman_er(prices, snapshot.get("er_window", 10))
        
        # ADX requires H/L/C
        # [Audit Layer 3] Use Session High/Low + Day High/Low for high-fidelity trend strength
        s_high = snapshot.get("session_high", spot)
        s_low = snapshot.get("session_low", spot)
        d_high = snapshot.get("day_high", s_high)
        d_low = snapshot.get("day_low", s_low)

        # Build local context high/low/close for ADX (Simplified 14-tick context)
        # We use the prices_series but inject the latest high-fidelity wicks at the end
        h_arr = np.concatenate([prices[:-1], [max(prices[-1], s_high, d_high)]])
        l_arr = np.concatenate([prices[:-1], [min(prices[-1], s_low, d_low)]])
        result["adx"] = calculate_adx(h_arr, l_arr, prices, 14) 

        # ── Smart Flow (Composite CMF + MFI) ──────────────────────────────
        vols = np.array(snapshot.get("vol_series", [1.0] * len(prices)))
        result["smart_flow"] = calculate_smart_flow(prices * 1.001, prices * 0.999, prices, vols)

        # ── Phase 12.1: Sentiment Fusion ───────────────────────────────────
        sentiment_score = snapshot.get("sentiment_score", 0.0) # -1.0 to 1.0
        result["sentiment_bias"] = float(sentiment_score)

        # ── [D-40] RSI-14 Calculation ──────────────────────────────────────
        rsi_win = rsi_windows[symbol]
        rsi_win.append(spot)
        if len(rsi_win) >= 15:
            delta = np.diff(list(rsi_win))
            gain = np.mean(delta[delta > 0]) if any(delta > 0) else 0.0
            loss = np.abs(np.mean(delta[delta < 0])) if any(delta < 0) else 0.0
            rs = gain / loss if loss > 0 else 100.0
            result["rsi"] = float(100 - (100 / (1 + rs)))
        else:
            result["rsi"] = 50.0

        # ── [D-41] PCR Calculation (Put-Call Ratio) ────────────────────────
        pe_oi = float(snapshot.get("pe_oi", 0))
        ce_oi = float(snapshot.get("ce_oi", 0))
        result["pcr"] = float(pe_oi / ce_oi) if ce_oi > 0 else 1.0
        
        # [Audit] PCR ROC (30s change rate)
        prev_pcr = float(snapshot.get("prev_pcr", result["pcr"]))
        result["pcr_roc"] = float((result["pcr"] - prev_pcr) / prev_pcr * 100) if prev_pcr > 0 else 0.0

        # ── [D-42] Change% Calculation ─────────────────────────────────────
        day_open = float(snapshot.get("day_open", spot))
        result["change_pct"] = float((spot - day_open) / day_open * 100) if day_open > 0 else 0.0

        # ── Individual Heavyweight Alpha Scores (for SHM) ──────────────────
        hw_alphas = {}
        if symbol == "NIFTY50" or symbol == "BANKNIFTY" or symbol == "SENSEX":
            for hw, prices in hw_prices.items():
                if len(prices) >= 20:
                    ret = np.diff(np.log(prices))
                    hw_alphas[hw] = float(np.mean(ret) / np.std(ret)) if np.std(ret) > 0 else 0.0
                else:
                    hw_alphas[hw] = 0.0
        result["hw_alphas"] = hw_alphas

        # ── ASTO Implementation (S23) ─────────────────────────────────────
        # Retrieve or initialize engine for this symbol
        engine = asto_engines[symbol]
        hist = atr_histories[symbol]
        
        # Calculate Z_vol (Z-score of ATR over 100-period window) for adaptive multiplier
        # We still use result["atr"] as a proxy for the 'base' activity level
        atr_val = result.get("atr", 20.0)
        hist.append(atr_val)
        
        if len(hist) >= 20: # Warm up window
            mu_atr = np.mean(hist)
            std_atr = np.std(hist)
            z_vol = float((atr_val - mu_atr) / std_atr) if std_atr > 0 else 0.0
        else:
            z_vol = 0.0
            
        # [Audit Layer 3] Use Session High/Low for zero-latency ASTO bands
        curr_high = snapshot.get("session_high", spot)
        curr_low = snapshot.get("session_low", spot)

        asto_raw, asto_regime, m_adaptive = engine.compute(curr_high, curr_low, spot, z_vol)
        result["asto"] = float(asto_raw)
        result["asto_regime"] = int(asto_regime)
        result["asto_multiplier"] = float(m_adaptive)
        
        # [Layer 3 Wick Mapping] Export raw wicks
        result["day_high"] = float(d_high)
        result["day_low"] = float(d_low)


        return result

    # Worker main loop
    while True:
        try:
            snapshot = in_queue.get(timeout=1.0)
            if snapshot is None:
                break  # Sentinel → shutdown
            signals = compute_signals(snapshot)
            out_queue.put(signals)
        except Empty:
            continue
        except Exception as e:
            logger.error(f"ComputeWorker error: {e}")


# ── Composite Alpha Scorer ────────────────────────────────────────────────────

class CompositeAlphaScorer:
    def __init__(self):
        self.weights = {"env": 0.20, "str": 0.30, "div": 0.50}

    def get_total_score(self, env_data: dict, str_data: dict, div_data: dict) -> float:
        now = datetime.now()
        hour = now.hour + now.minute / 60.0
        multiplier = 0.5 if (11.5 <= hour <= 13.5) or (hour >= 15.0) else 1.0

        s_env = self._calc_env(env_data)
        s_str = self._calc_str(str_data)
        s_div = self._calc_div(div_data)
        
        # Phase 12.1: Sentiment Multiplier
        sentiment_score = env_data.get("sentiment_score", 0.0)
        sentiment_mult = 1.0 + (0.2 * sentiment_score) # +/- 20% impact
        
        total = (self.weights["env"] * s_env + self.weights["str"] * s_str +
                 self.weights["div"] * s_div) * multiplier * sentiment_mult
        return float(np.clip(total, -100, 100))

    def _calc_env(self, d: dict) -> float:
        score = d.get("fii_bias", 0)
        score += -10 * d.get("vix_slope", 0)
        if d.get("ivp", 50) > 80:
            return -100.0
        if d.get("ivp", 50) < 20:
            score += 20
        return float(score)

    def _calc_str(self, d: dict) -> float:
        score = 50 * d.get("basis_slope", 0)
        score += -5 * d.get("dist_max_pain", 0)
        pcr = d.get("pcr", 1.0)
        if pcr > 1.3:
            score -= 20
        if pcr < 0.7:
            score += 20
        return float(score)

    def _calc_div(self, d: dict) -> float:
        score = 0.0
        if d.get("price_slope", 0) > 0 and d.get("pcr_slope", 0) < 0:
            score -= 50
        if d.get("price_slope", 0) < 0 and d.get("cvd_slope", 0) > 0:
            score += 50
        return score

# ── Main Market Sensor ────────────────────────────────────────────────────────

class MarketSensor:
    def __init__(self, test_mode: bool = False):
        self.all_indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
        self.mq = MQManager()
        self.test_mode = test_mode
        self.scorer = CompositeAlphaScorer()

        redis_host = os.getenv("REDIS_HOST", "localhost")
        if not test_mode:
            self.pub = self.mq.create_publisher(Ports.MARKET_STATE)
            redis_pass = os.getenv("REDIS_PASSWORD", "")
            auth_str = f":{redis_pass}@" if redis_pass else ""
            self._redis = redis.from_url(f"redis://{auth_str}{redis_host}:6379", decode_responses=True)
            self.shm_managers = {
                idx: ShmManager(asset_id=idx, mode='w') for idx in self.all_indices
            }
            # Also keep a global one for legacy if needed, or just use NIFTY50 as default
            self.shm_global = ShmManager(asset_id="GLOBAL", mode='w')
        else:
            self.shm_managers = {}
            self.shm_global = None
            redis_pass = os.getenv("REDIS_PASSWORD", "")
            auth_str = f":{redis_pass}@" if redis_pass else ""
            self._redis = redis.from_url(f"redis://{auth_str}{redis_host}:6379", decode_responses=True)

        self.manager: Any = None
        self.main_task: asyncio.Task | None = None
        self.hb: HeartbeatProvider | None = None

        # High-performance Rust engine integration
        self.use_rust = os.getenv("USE_RUST_ENGINE", "0") == "1" and HAS_RUST_ENGINE
        if self.use_rust:
            logger.info("🚀 Rust Tick Engine ACTIVATED (Bypassing GIL / mp.Process)")
            self.rust_engine = tick_engine.TickEngine(vpin_bucket_size=5000)  # [D-01] Spec: 5000, was 100
        else:
            self.rust_engine = None
            logger.info("🐍 Using standard Python ComputeWorker (Multiprocessing mode)")
            # self._redis is already initialized above

        # Tick buffers (main process — lightweight)
        # Audit 1.2: Explicitly type hint deques to avoid list[Error] lints
        self.tick_store: dict[str, collections.deque[dict[str, Any]]] = collections.defaultdict(
            lambda: collections.deque(maxlen=2000)
        )
        self.all_indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
        all_assets = self.all_indices + TOP_10_HEAVYWEIGHTS
        self.hw_prices: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=500) for sym in all_assets
        }
        self.ofi_series: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=200) for sym in all_assets
        }
        self.vix_series: collections.deque[float] = collections.deque(maxlen=300) # 5m @ 1s pub - Keep global as VIX is cross-asset
        self.cvd: dict[str, float] = collections.defaultdict(float)
        self.cvd_series: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=200) for sym in all_assets
        }
        self.basis_series: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=500) for sym in all_assets
        }
        self.spot_15m_series: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=900) for sym in all_assets
        }

        # [Audit Layer 3] Session-derived wicks for zero-latency math
        self.session_high: dict[str, float] = collections.defaultdict(lambda: -1.0)
        self.session_low: dict[str, float] = collections.defaultdict(lambda: 1e9)

        # VPIN State (SRS Phase 2) - Asset Scoped
        self.vpin_bucket_size = 5000  # [D-01] Spec: 5000 volume units per bucket, was 100
        self.vpin_current_vol: dict[str, int] = collections.defaultdict(int)
        self.vpin_buy_vol: dict[str, int] = collections.defaultdict(int)
        self.vpin_sell_vol: dict[str, int] = collections.defaultdict(int)
        self.vpin_series: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=50) for sym in all_assets
        }
        self.iv_history: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=100) for sym in all_assets
        }
        self.pcr_history: dict[str, collections.deque[float]] = {
            sym: collections.deque(maxlen=100) for sym in all_assets
        }
        self._last_tick_ts: dict[str, float] = collections.defaultdict(time.time)

        # ASTO Indicators (SRS Part 1)
        self.atr_buffer: collections.deque[float] = collections.deque(maxlen=10)
        self.asto_engine = AdaptiveSuperTrendOscillator(base_multiplier=3.0, sensitivity=0.5)

        # Compute subprocess setup
        self._compute_in: mp.Queue = mp.Queue(maxsize=50)
        self._compute_out: mp.Queue = mp.Queue(maxsize=50)
        self._latest_signals: dict[str, Any] = {}
        self._compute_proc: mp.Process | None = None

        # Hurst exponent cache
        self._last_hurst_calc = 0.0

        # [Hedge Hybrid] Holistic state
        self.market_states: dict[str, str] = collections.defaultdict(lambda: "NEUTRAL")
        self.vwap_cum_pv: dict[str, float] = collections.defaultdict(float)
        self.vwap_cum_vol: dict[str, float] = collections.defaultdict(float)
        
        # [Audit-Fix] Option OI Store for Walls Calculation
        self.option_oi: dict[str, dict[str, dict[int, float]]] = collections.defaultdict(
            lambda: {"CE": {}, "PE": {}}
        )
        self._last_wall_pub = 0.0
        self.off_hour_sim = os.getenv("ENABLE_OFF_HOUR_SIMULATOR", "false").lower() == "true"
        self._sim_snap_prices: dict[str, float] = {}   # [Audit-Fix] For IV stabilization
        
        # [Audit-Fix] Signal State Persistence
        self.last_sign: dict[str, float] = collections.defaultdict(float) # Lee-Ready Tie-Breaker
        self.last_sequence_id: dict[str, int] = collections.defaultdict(int)
        self.prev_quotes: dict[str, dict] = collections.defaultdict(dict) # For OFI Liquidity Flow

    # Removed redundant compute process methods

    async def start(self):
        """Starts multiprocessing sensors and the async state publishing."""
        try:
            self.manager = mp.Manager()
            self._compute_in = self.manager.Queue(maxsize=10000) # Re-initialize with manager queue
            self._compute_out = self.manager.Queue(maxsize=1000) # Re-initialize with manager queue
            
            self._compute_proc = mp.Process(
                target=_compute_worker,
                args=(self._compute_in, self._compute_out),
                daemon=True,
                name="MarketSensor_Compute"
            )
            self._compute_proc.start() # type: ignore
            logger.info(f"🚀 Compute Process started. PID: {self._compute_proc.pid}")
            
            # [Audit 11.1] Heartbeat integration
            self.hb = HeartbeatProvider("MarketSensor", self._redis)
            asyncio.create_task(self.hb.run_heartbeat())

            await self.run()
        except Exception as e:
            logger.error(f"❌ Failed to start Market Sensor: {e}")
            self.stop()
            raise

    def stop(self):
        """Gracefully stops all loops and background processes."""
        logger.info("🛑 Stopping Market Sensor...")
        if hasattr(self, 'main_task') and self.main_task:
            try:
                self.main_task.cancel()
            except Exception:
                pass
        if self.hb:
            self.hb.stop_heartbeat()
        if self._compute_proc:
            if self._compute_proc.is_alive():
                logger.info("Waiting for compute process to finish...")
                # Send sentinel to compute process to shut down gracefully (Non-blocking)
                try:
                    self._compute_in.put_nowait(None)
                except Exception as e:
                    logger.warning(f"Could not send sentinel to compute process: {e}")
                self._compute_proc.join(timeout=2)
                if self._compute_proc.is_alive():
                    logger.warning("Compute process did not terminate gracefully, forcing kill.")
                    self._compute_proc.terminate()
        logger.info("✅ Market Sensor stopped successfully.")

    def _pin_core(self):
        if sys.platform != "win32":
            try:
                os.sched_setaffinity(0, {0, 1}) # Pin to first two cores
                logger.info("Pinned Market Sensor to cores 0,1 natively.")
            except Exception as e:
                logger.error(f"Failed to pin core: {e}")

    def _classify_trade(self, tick: dict) -> float:
        """
        Lee-Ready Trade Classification (SRS §2.5).
        Classifies LTP relative to mid-quote to filter bid-ask bounce noise.
        """
        ltp = tick.get("price", 0.0)
        bid = tick.get("bid", 0.0)
        ask = tick.get("ask", 0.0)
        
        if bid <= 0 or ask <= 0:
            return 0.0
            
        mid = (bid + ask) / 2.0
        
        if ltp > mid:
            sign = 1.0  # Aggressive Buy
        elif ltp < mid:
            sign = -1.0 # Aggressive Sell
        else:
            # [Audit-Fix] Lee-Ready Tie-Breaker: Use last known sign (Tick Rule)
            sign = self.last_sign.get(tick.get("symbol", ""), 0.0)
        
        if sign != 0:
            self.last_sign[tick.get("symbol", "")] = sign

        if self.use_rust:
            rt = tick_engine.TickData(
                tick.get("price", 0.0),
                tick.get("bid", 0.0),
                tick.get("ask", 0.0),
                tick.get("last_volume", 1)
            )
            return self.rust_engine.classify_trade(rt) * tick.get("last_volume", 1)
        
        volume = tick.get("last_volume", 1)
        return sign * volume

    def _ofi(self, tick: dict) -> float:
        """
        [Audit-Fix] Order Flow Imbalance (OFI) - Liquidity Flow Standard.
        Measures changes in liquidity (quantity added/removed) rather than just static depth.
        """
        symbol = tick.get("symbol", "NIFTY50")
        bid = tick.get("bid", 0.0)
        ask = tick.get("ask", 0.0)
        bid_size = tick.get("bid_size", 0)
        ask_size = tick.get("ask_size", 0)
        
        prev = self.prev_quotes.get(symbol, {})
        prev_bid = prev.get("bid", bid)
        prev_ask = prev.get("ask", ask)
        prev_bid_size = prev.get("bid_size", bid_size)
        prev_ask_size = prev.get("ask_size", ask_size)
        
        # 1. Delta Bid (Liquidity added at or above the previous bid)
        if bid > prev_bid:
            delta_bid = bid_size
        elif bid == prev_bid:
            delta_bid = bid_size - prev_bid_size
        else:
            delta_bid = -prev_bid_size
            
        # 2. Delta Ask (Liquidity added at or below the previous ask)
        if ask < prev_ask:
            delta_ask = ask_size
        elif ask == prev_ask:
            delta_ask = ask_size - prev_ask_size
        else:
            delta_ask = -prev_ask_size
            
        # Store for next tick
        self.prev_quotes[symbol] = {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size}
        
        return float(delta_bid - delta_ask)

    def _update_cvd(self, tick: dict, symbol: str):
        """Cumulative Volume Delta update using Lee-Ready classification."""
        sign = self._classify_trade(tick)
        # volume = tick.get("last_volume", 1) # classification already includes volume conceptually
        self.cvd[symbol] += sign 
        self.cvd_series[symbol].append(self.cvd[symbol])

    def _update_vpin(self, tick: dict, symbol: str):
        """Updates VPIN volume buckets and calculates VPIN on bucket completion."""
        if self.use_rust:
            rt = tick_engine.TickData(
                tick.get("price", 0.0),
                tick.get("bid", 0.0),
                tick.get("ask", 0.0),
                tick.get("last_volume", 1)
            )
            vpin_val = self.rust_engine.update_vpin(rt)
            if vpin_val is not None:
                self.vpin_series[symbol].append(vpin_val)
            return

        sign = self._classify_trade(tick)
        volume = tick.get("last_volume", 1)
        
        self.vpin_current_vol[symbol] += volume
        if sign > 0:
            self.vpin_buy_vol[symbol] += volume
        elif sign < 0:
            self.vpin_sell_vol[symbol] += volume
            
        # [Audit-Fix] VPIN Bucket Overflow: Use 'while' to drain excessive volume in large ticks
        while self.vpin_current_vol[symbol] >= self.vpin_bucket_size:
            # Calculate ratio based on bucket size, not total volume (to maintain precision)
            # We use a proportional split for the current bucket
            ratio = self.vpin_bucket_size / self.vpin_current_vol[symbol]
            b_buy = self.vpin_buy_vol[symbol] * ratio
            b_sell = self.vpin_sell_vol[symbol] * ratio
            
            vpin_val = abs(b_buy - b_sell) / self.vpin_bucket_size
            self.vpin_series[symbol].append(vpin_val)
            
            # Substract bucket_size and move remainder to next interval
            self.vpin_current_vol[symbol] -= self.vpin_bucket_size
            self.vpin_buy_vol[symbol] -= b_buy
            self.vpin_sell_vol[symbol] -= b_sell

    async def _drain_compute_output(self):
        """Non-blocking drain of compute_out queue into latest_signals."""
        while True:
            try:
                msg = self._compute_out.get_nowait()
                if isinstance(msg, dict):
                    if "symbol" in msg:
                        # Signal for a specific symbol (e.g. heavyweight)
                        sym = msg["symbol"]
                        if "signals" in msg:
                            self._latest_signals[sym] = msg["signals"]
                    else:
                        # Global or default symbol signals
                        self._latest_signals.update(msg)
            except queue.Empty:
                break

    def _handle_sequence_reset(self, symbol: str):
        """
        Clears only the 'differential' buffers that rely on tick-to-tick continuity.
        Historical price deques remain intact for trend continuity.
        """
        self.prev_quotes.pop(symbol, None) # Resets OFI baseline
        self.last_sign.pop(symbol, None)   # Resets Lee-Ready tie-breaker
        # VPIN stays as-is (it will just complete a slightly 'dirty' bucket)

    async def _initialize_redis_state(self):
        """[Audit-Fix] Adaptive Alpha: Hydrate dynamic components and VWAP state from Redis."""
        logger.info("📐 MarketSensor: Adopting session's dynamic constituent profile...")
        
        # 1. Fetch dynamic components for each index
        global INDEX_COMPONENTS
        updated_any = False
        for idx in self.all_indices:
            try:
                cached = await self._redis.get(f"CONFIG:COMPONENTS:{idx}")
                if cached:
                    components = json.loads(cached)
                    INDEX_COMPONENTS[idx] = components
                    updated_any = True
                    logger.info(f"✅ {idx}: Adopted {len(components)} dynamic constituents.")
            except Exception as e:
                logger.warning(f"Failed to load dynamic components for {idx}: {e}")

        if updated_any:
            # Update buffers for any NEW symbols found in dynamic components
            new_heavyweights = set()
            for components in INDEX_COMPONENTS.values():
                new_heavyweights.update(components)
            
            all_assets = self.all_indices + list(new_heavyweights)
            for sym in all_assets:
                if sym not in self.hw_prices:
                    self.hw_prices[sym] = collections.deque(maxlen=500)
                    self.ofi_series[sym] = collections.deque(maxlen=200)
                    self.cvd_series[sym] = collections.deque(maxlen=200)
                    self.basis_series[sym] = collections.deque(maxlen=500)
                    self.spot_15m_series[sym] = collections.deque(maxlen=900)
                    self.vpin_series[sym] = collections.deque(maxlen=50)
                    self.iv_history[sym] = collections.deque(maxlen=100)
                    self.pcr_history[sym] = collections.deque(maxlen=100)
            logger.info(f"📐 Alpha Surface recalibrated for {len(all_assets)} assets.")

        # 2. SEBI/VWAP State Recovery
        assets = ["NIFTY50", "BANKNIFTY", "SENSEX"]
        for asset in assets:
            if not await self._redis.exists(f"latest_market_state:{asset}"):
                initial_state = {
                    "symbol": asset,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "price": 0.0,
                    "s_total": 0.0,
                    "regime": "INITIALIZING",
                    "status": "AWAITING_TICKS"
                }
                await self._redis.set(f"latest_market_state:{asset}", json.dumps(initial_state, cls=NumpyEncoder))
                logger.info(f"Initialized Redis state for {asset}")
            
            # [Audit-Fix] VWAP Hydration (Cold Start Fix)
            # Fetch persisted PV and Vol from Redis if they exist
            pv = await self._redis.get(f"VWAP_PV:{asset}")
            vol = await self._redis.get(f"VWAP_VOL:{asset}")
            if pv and vol:
                self.vwap_cum_pv[asset] = float(pv)
                self.vwap_cum_vol[asset] = float(vol)
                logger.info(f"Hydrated VWAP for {asset}: {self.vwap_cum_pv[asset] / self.vwap_cum_vol[asset]:.2f}")

    async def run(self):
        # Initialize Redis state for each symbol on boot
        # This prevents (nil) errors in the dashboard before the first 50 ticks arrive.
        await self._initialize_redis_state()

        if not self.test_mode:
            # The _start_compute_process is now handled by the new `start` method
            pass

        # ── Phase 9: UI & Observability heartbeat ──
        # [Audit-Fix] Already initialized in start() method. Bypassing to avoid race condition.
        # self.hb = HeartbeatProvider("MarketSensor", self._redis)
        # asyncio.create_task(self.hb.run_heartbeat())

        logger.info("MarketSensor active. Subscribing to tick data...")
        asyncio.create_task(send_cloud_alert("👁️ MARKET SENSOR: Active. Computing microstructure features and correlations.", alert_type="SYSTEM"))
        sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA, "TICK."])
        tick_count = 0

        try:
            while True:
                try:
                    topic, tick = await self.mq.recv_json(sub)
                    if not tick:
                        await asyncio.sleep(0.01)
                        continue

                    symbol = tick.get("symbol", "NIFTY50")
                    
                    # [Fortress] Sequence Reset Detector (Audit 2.3)
                    incoming_seq = tick.get("sequence_id", 0)
                    last_seq = self.last_sequence_id.get(symbol, 0)

                    if incoming_seq < last_seq - 100:
                        logger.warning(f"🔄 GATEWAY RESET DETECTED for {symbol}: {last_seq} -> {incoming_seq}. Re-centering buffers.")
                        self._handle_sequence_reset(symbol)

                    self.last_sequence_id[symbol] = incoming_seq

                    price = tick.get("price", 0.0)

                    # [Audit Layer 3] Running Session High/Low
                    # Initialize if first tick for symbol
                    if symbol not in self.session_high:
                        self.session_high[symbol] = price
                        self.session_low[symbol] = price
                    self.session_high[symbol] = max(self.session_high[symbol], price)
                    self.session_low[symbol] = min(self.session_low[symbol], price)

                    # Store tick
                    self.tick_store[symbol].append(tick)

                    # Update heavyweight price buffer for dispersion
                    if symbol in TOP_10_HEAVYWEIGHTS:
                        self.hw_prices[symbol].append(price)

                    # Update OFI, CVD, VPIN, basis
                    self.ofi_series[symbol].append(self._ofi(tick))
                    self._update_cvd(tick, symbol)
                    
                    # [Audit Fix] Asset-specific logic for VPIN, Basis, and 15m Slope
                    self._update_vpin(tick, symbol)
                    self._last_tick_ts[symbol] = time.time()

                    # Simulate basis (futures_price - spot)
                    futures_est = price * (1 + 0.0005 * (np.random.random() - 0.5))
                    self.basis_series[symbol].append(futures_est - price)

                    # [Audit Fix] 15m Spot series for all indices
                    if symbol in self.all_indices:
                        self.spot_15m_series[symbol].append(float(price))

                    # [Audit-Fix] Option OI Tracking for Walls
                    if " " in symbol and (symbol.endswith(" CE") or symbol.endswith(" PE")):
                        try:
                            parts = symbol.split()
                            # Format: "NIFTY 26MAR 22000 CE"
                            base = parts[0]
                            strike = int(parts[2])
                            otype = parts[3]
                            oi = float(tick.get("oi", 0))
                            if oi > 0:
                                self.option_oi[base][otype][strike] = oi
                        except (IndexError, ValueError):
                            pass

                    tick_count = int(tick_count + 1)

                    # Drain compute results every tick (non-blocking)
                    await self._drain_compute_output()

                    # Send snapshot to compute process every 20 ticks
                    if tick_count % 20 == 0 and not self.test_mode:
                        price_series = [t.get("price", price) for t in list(self.tick_store[symbol])]
                        # [D-43] Fetch Raw Data for Signal Expansion
                        base_asset = symbol.replace("50", "") if "NIFTY" in symbol else symbol
                        day_open = await self._redis.get(f"DAY_OPEN:{symbol}")
                        ce_oi = await self._redis.get(f"OI:CE:{base_asset}")
                        pe_oi = await self._redis.get(f"OI:PE:{base_asset}")

                        snapshot = {
                            "symbol": symbol,
                            "spot": price,
                            "day_open": float(day_open) if day_open else price,
                            "ce_oi": float(ce_oi) if ce_oi else 0.0,
                            "pe_oi": float(pe_oi) if pe_oi else 0.0,
                            "session_high": self.session_high[symbol],
                            "session_low": self.session_low[symbol],
                            "day_high": float(tick.get("day_high", self.session_high[symbol])),
                            "day_low": float(tick.get("day_low", self.session_low[symbol])),
                            "ofi_series": list(self.ofi_series[symbol]),
                            "hw_prices": {k: list(v) for k, v in self.hw_prices.items() if len(v) >= 10},
                            "cvd_series": list(self.cvd_series[symbol]),
                            "basis_series": list(self.basis_series[symbol]),
                            "spot_15m_series": list(self.spot_15m_series[symbol]),
                            "vpin_series": list(self.vpin_series[symbol]) if self.vpin_series[symbol] else [0.0],
                            "vol_series": [t.get("volume", 1) for t in list(self.tick_store[symbol])][-200:],
                            "iv_history": list(self.iv_history[symbol]),
                            "prev_pcr": self.pcr_history[symbol][-1] if self.pcr_history[symbol] else 1.0,
                            "zero_gamma_level": find_zero_gamma_level(np.array(price_series), price),
                            "price_series": price_series,
                            "hurst_val": calculate_hurst(np.array(price_series[-500:])) if len(price_series) >= 50 else 0.5,
                            "er_window": 10, # Dynamic via Firestore later
                            "strikes": [price - 200, price - 100, price, price + 100, price + 200],
                            "near_term_iv": await self._redis.get("atm_iv") or 0.20,
                            "far_term_iv": 0.17,
                            "atm_iv": await self._redis.get("atm_iv") or 0.18,
                            "dte": 2,
                            "off_hour_sim": self.off_hour_sim,
                            "sim_snap_price": self._sim_snap_prices.get(symbol, price),
                            "sentiment_score": float(await self._redis.get("news_sentiment_score") or 0.0),
                            "risk_free_rate": float(await self._redis.get("CONFIG:RISK_FREE_RATE") or 0.065)
                        }
                        if symbol not in self._sim_snap_prices:
                            self._sim_snap_prices[symbol] = price
                        try:
                            self._compute_in.put_nowait(snapshot)
                        except (queue.Full, Exception):
                            pass  # Skip if compute is backed up — don't block I/O loop

                        # GAP FIX: Also send individual snapshots for heavyweights for Power Five matrix
                        if symbol in TOP_10_HEAVYWEIGHTS:
                            hw_snapshot = {
                                "symbol": symbol,
                                "spot": price,
                                "price_series": price_series,
                                "ofi_series": [t.get("price", price) for t in list(self.tick_store[symbol])][-100:]
                            }
                            try:
                                self._compute_in.put_nowait(hw_snapshot)
                            except (queue.Full, Exception):
                                pass # Skip if compute is backed up

                    # Publish state every 50 ticks
                    if tick_count % 50 == 0:
                        await self._publish_market_state(symbol, price)

                    await asyncio.sleep(0)  # yield to event loop

                except zmq.Again:
                    continue
                except Exception as e:
                    logger.error(f"Market Sensor loop error: {e}")
                    await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Market Sensor outer catastrophic error: {e}")
        finally:
            sub.close()

    async def _publish_market_state(self, symbol: str, price: float):
        """Assembles and publishes the full market state vector."""
        market_signal_state = {}
        sig = self._latest_signals.get(symbol, {})

        # Retrieve FII bias and Sentiment from Redis
        try:
            fii_bias_val = await self._redis.get("fii_bias")
            fii_bias = float(fii_bias_val or 0)
            sentiment_score = float(await self._redis.get("news_sentiment_score") or 0.0)
        except Exception:
            fii_bias = 0.0
            sentiment_score = 0.0

        prices_arr = np.array([t.get("price", price) for t in list(self.tick_store[symbol])])

        env_data = {"fii_bias": fii_bias, "vix_slope": 0.01, "ivp": 25, "sentiment_score": sentiment_score}
        
        basis_list = list(self.basis_series[symbol])
        curr_pcr = float(sig.get("pcr", 0.85))
        str_data = {"basis_slope": float(np.mean(basis_list[-5:]) if len(basis_list) >= 5 else 0.0),
                    "dist_max_pain": 10.0, "pcr": curr_pcr}
        
        cvd_list = list(self.cvd_series[symbol])
        div_data = {"price_slope": float(np.diff(prices_arr[-10:]).mean()) if len(prices_arr) >= 10 else 0.0,
                    "pcr_slope": 0.02, 
                    "cvd_slope": float(np.diff(cvd_list[-5:]).mean()) if len(cvd_list) >= 5 else 0.0}

        s_env = self.scorer._calc_env(env_data)
        s_str = self.scorer._calc_str(str_data)
        s_div = self.scorer._calc_div(div_data)
        s_total = self.scorer.get_total_score(env_data, str_data, div_data)

        vpin = sig.get("vpin", 0.0)
        flow_toxicity_veto = sig.get("flow_toxicity_veto", False)

        # [Fast Lane] Extract Depth & Calculate Pressure Gauge (S04)
        last_tick = self.tick_store[symbol][-1] if self.tick_store[symbol] else {}
        tb = float(last_tick.get("total_buy_qty", 0))
        ts = float(last_tick.get("total_sell_qty", 0))
        pressure = (tb - ts) / (tb + ts) if (tb + ts) > 0 else 0.0

        # [Slow Lane] Fetch Walls from Redis
        call_wall = await self._redis.get(f"CALL_WALL:{symbol}") or "—"
        put_wall = await self._redis.get(f"PUT_WALL:{symbol}") or "—"

        market_signal_state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "price": price,
            "total_buy_qty": tb,
            "total_sell_qty": ts,
            "pressure_gauge": round(pressure, 4),
            "call_wall": call_wall,
            "put_wall": put_wall,
            "s_total": s_total,
            "s_env": s_env,
            "s_str": s_str,
            "s_div": s_div,
            "hurst": sig.get("hurst", 0.5),
            "kaufman_er": sig.get("kaufman_er", 0.5),
            "adx": sig.get("adx", 20.0),
            "rv": sig.get("rv_10d", 0.18),
            "iv_atm": float(await self._redis.get("atm_iv") or 0.18),
            "iv_percentile": sig.get("iv_percentile", 50.0),
            "iv_rv_spread": sig.get("iv_rv_spread", 0.0),
            "high_z": sig.get("high_z", 0.0),
            "low_z": sig.get("low_z", 0.0),
            "day_high": sig.get("day_high", price),
            "day_low": sig.get("day_low", price),
            "smart_flow": sig.get("smart_flow", 0.0),
            "pcr_roc": sig.get("pcr_roc", 0.0),
            "log_ofi_zscore": sig.get("log_ofi_zscore", 0.0),
            "dispersion_coeff": sig.get("dispersion_coeff", 0.5),
            "vol_term_ratio": sig.get("vol_term_ratio", 1.0),
            "zero_gamma_level": sig.get("zero_gamma_level", price),
            "gex_sign": sig.get("gex_sign", "POSITIVE"),
            "charm": sig.get("charm", 0.0),
            "vanna": sig.get("vanna", 0.0),
            "toxic_option": sig.get("toxic_option", False),
            "atr": sig.get("atr", 20.0),
            "cvd_absorption": sig.get("cvd_absorption", False),
            "cvd_flip_ticks": sig.get("cvd_flip_ticks", 0),
            "basis_zscore": sig.get("basis_zscore", 0.0),
            "price_dislocation": sig.get("price_dislocation", False),
            "spot_zscore_15m": sig.get("spot_zscore_15m", 0.0),
            "vpin": vpin,
            "cvd": float(self.cvd[symbol]),
            "asto": sig.get("asto", 0.0),
            "asto_regime": sig.get("asto_regime", 0),
            "asto_multiplier": sig.get("asto_multiplier", 3.0),
            "flow_toxicity_veto": flow_toxicity_veto,
            "sentiment_score": sentiment_score,
            "rsi": float(sig.get("rsi", 50.0)),
            "pcr": curr_pcr,
            "change_pct": float(sig.get("change_pct", 0.0)),
            "latency_ms": float(last_tick.get("latency_ms", 0.0)),
            "time_of_day": datetime.now().strftime("%H:%M:%S")
        }

        # [Audit] RAW_VETO and STALE_DATA_FLAG logic
        sys_halt = await self._redis.get("SYSTEM_HALT") or "False"
        raw_veto = (vpin > 0.8) or (sys_halt != "False")
        stale_flag = (time.time() - self._last_tick_ts.get(symbol, 0) > 10)
        heartbeat = time.time()
        
        market_signal_state["raw_veto"] = bool(raw_veto)
        market_signal_state["stale_data_flag"] = bool(stale_flag)
        market_signal_state["heartbeat"] = heartbeat

        # Update histories for next snapshot
        self.iv_history[symbol].append(market_signal_state["iv_atm"])
        self.pcr_history[symbol].append(market_signal_state["pcr"])

        # ── [Hedge Hybrid] Three-State Deterministic Model (S22 & MARKET_STATE) ──
        asto = float(market_signal_state.get("asto", 0.0))
        abs_asto = abs(asto)
        
        # 1. Update Anchored VWAP (resets at sensor start)
        vol = float(last_tick.get("last_volume", 1))
        
        self.vwap_cum_pv[symbol] = self.vwap_cum_pv.get(symbol, 0.0) + (price * vol)
        self.vwap_cum_vol[symbol] = self.vwap_cum_vol.get(symbol, 0.0) + vol
        
        # [Audit-Fix] Persist VWAP state to Redis for crash durability
        await self._redis.set(f"VWAP_PV:{symbol}", str(self.vwap_cum_pv[symbol]))
        await self._redis.set(f"VWAP_VOL:{symbol}", str(self.vwap_cum_vol[symbol]))
        
        vwap = self.vwap_cum_pv[symbol] / self.vwap_cum_vol[symbol] if self.vwap_cum_vol[symbol] > 0 else price
        
        # 2. Calculate 15-min Price Slope
        slope_15m = 0.0
        if len(self.spot_15m_series[symbol]) >= 60: # at least 1 min of data for a slope
            s15 = list(self.spot_15m_series[symbol])
            slope_15m = float(s15[-1]) - float(s15[-60]) # point-to-point 1m delta as slope proxy
            
        # 3. Calculate Whale Pivot (S22)
        s22 = 0.0
        if price > vwap and slope_15m > 0:
            s22 = 1.0
        elif price < vwap and slope_15m < 0:
            s22 = -1.0
        
        # [Audit-Fix] Naming alignment with Dashboard (Plural)
        market_signal_state["whales_pivot"] = s22 # Plural for main.py
        market_signal_state["whale_pivot"] = s22  # Singular for SignalVector SHM

        # 4. Update MARKET_STATE with Hysteresis - Asset Scoped [Audit Fix]
        current_state = str(await self._redis.get(f"MARKET_STATE:{symbol}") or "NEUTRAL")
        new_state = current_state
        
        if abs_asto >= 90:
            new_state = f"EXTREME_TREND:{'BULLISH' if asto >= 90 else 'BEARISH'}"
        elif abs_asto >= 70:
            if "EXTREME" not in current_state:
                new_state = "TRENDING"
        else: # abs_asto < 70
            new_state = "NEUTRAL"
            
        market_signal_state["market_state"] = new_state
        self.market_states[symbol] = new_state
        await self._redis.set(f"MARKET_STATE:{symbol}", new_state)
        
        if abs_asto >= 90:
            await self._redis.set(f"MARKET_STATE:EXTREME_TREND:{symbol}", "TRUE")
        elif abs_asto <= 50:
             await self._redis.set(f"MARKET_STATE:EXTREME_TREND:{symbol}", "FALSE")

        # ── [R3-04] Publish India VIX to Redis ─────────────────────────
        # India VIX approximated from ATM IV when direct feed unavailable.
        if symbol == "NIFTY50":
            try:
                atm_iv_raw = await self._redis.get("atm_iv")
                if atm_iv_raw:
                    # ATM IV stored as decimal (0.18 = 18%), VIX as percentage (18.0)
                    vix_estimate = float(atm_iv_raw) * 100.0
                    await self._redis.set("vix", f"{vix_estimate:.2f}")
                    self.vix_series.append(vix_estimate)
                    
                    # [Audit-Fix] VIX Spike Veto (> 15% 5m expansion)
                    if len(self.vix_series) >= 300: # Full 5m window
                        vix_start = self.vix_series[0]
                        vix_now = self.vix_series[-1]
                        if vix_start > 0:
                            expansion = (vix_now - vix_start) / vix_start
                            if expansion > 0.15:
                                logger.critical(f"🚨 VIX SPIKE: {expansion*100:.1f}% expansion in 5m! Setting VIX_SPIKE_DETECTED.")
                                await self._redis.set("VIX_SPIKE_DETECTED", "True", ex=300) # 5m veto
                            else:
                                # Self-healing if spike subsides
                                await self._redis.set("VIX_SPIKE_DETECTED", "False")

            except Exception as e:
                logger.warning(f"Failed to publish VIX estimate to Redis: {e}")

        # ── Multi-Index Signal Publication ──────────────────────────
        if symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
            # Target 1 = entry + 1.5 * ATR, Target 2 = entry + 3 * ATR
            atr = float(market_signal_state.get("atr", 20.0))
            entry_val = await self._redis.get(f"entry_price:{symbol}")
            entry = float(entry_val) if entry_val else price
            
            side = await self._redis.get(f"active_side:{symbol}") or "LONG"
            mult = 1.0 if side == "LONG" else -1.0
            
            tp1 = float(entry) + (1.5 * atr * mult)
            tp2 = float(entry) + (3.0 * atr * mult)
            
            # Progress: how close are we to TP2 from entry
            dist_total = abs(tp2 - entry)
            dist_current = abs(price - entry)
            progress = min(100.0, max(0.0, (dist_current / dist_total) * 100.0)) if dist_total > 0 else 0.0
            
            market_signal_state["exit_path_70_30"] = {
                "tp1": round(float(tp1), 2),
                "tp2": round(float(tp2), 2),
                "progress": round(float(progress), 2)
            }

            if self.shm_managers:
                # Map heavyweight alpha scores from latest signals
                hw_list = [0.0] * 10
                hw_scores = self._latest_signals.get("hw_alphas", {})
                for idx, sym in enumerate(TOP_10_HEAVYWEIGHTS):
                    hw_list[idx] = float(hw_scores.get(sym, 0.0))

                # [Audit 14.2] Fetch net deltas for signal vector
                nd_nifty = float(await self._redis.get("net_delta_nifty50") or 0.0)
                nd_bank = float(await self._redis.get("net_delta_banknifty") or 0.0)
                nd_sensex = float(await self._redis.get("net_delta_sensex") or 0.0)

                signals = SignalVector(
                    s_total=market_signal_state["s_total"],
                    vpin=market_signal_state["vpin"],
                    ofi_z=market_signal_state["log_ofi_zscore"],
                    vanna=market_signal_state.get("vanna", 0.0),
                    charm=market_signal_state.get("charm", 0.0),
                    s_env=market_signal_state.get("s_env", 0.0),
                    s_str=market_signal_state.get("s_str", 0.0),
                    s_div=market_signal_state.get("s_div", 0.0),
                    rv=market_signal_state["rv"],
                    adx=market_signal_state["adx"],
                    pcr=market_signal_state.get("pcr", curr_pcr),
                    asto=market_signal_state["asto"],
                    asto_regime=market_signal_state["asto_regime"],
                    whale_pivot=market_signal_state["whale_pivot"],
                    net_delta_nifty=nd_nifty,
                    net_delta_banknifty=nd_bank,
                    net_delta_sensex=nd_sensex,
                    # Extended Fields
                    high_z=sig.get("high_z", 0.0),
                    low_z=sig.get("low_z", 0.0),
                    iv_percentile=sig.get("iv_percentile", 50.0),
                    iv_rv_spread=sig.get("iv_rv_spread", 0.0),
                    smart_flow=sig.get("smart_flow", 0.0),
                    buy_p=tb,
                    sell_p=ts,
                    pcr_roc=sig.get("pcr_roc", 0.0),
                    iv_atm=market_signal_state.get("iv_atm", 0.0),
                    latency_ms=market_signal_state.get("latency_ms", 0.0),
                    day_high=sig.get("day_high", price),
                    day_low=sig.get("day_low", price),
                    hb_ts=market_signal_state.get("heartbeat", 0.0),
                    veto=bool(market_signal_state.get("flow_toxicity_veto", False)),
                    raw_veto=market_signal_state.get("raw_veto", False),
                    stale_flag=market_signal_state.get("stale_data_flag", False),
                    hw_alpha=hw_list
                )
                
                # Write to asset-specific SHM
                if symbol in self.shm_managers:
                    self.shm_managers[symbol].write(signals)
                
                # Also write to global if it's a primary index for MetaRouter/StrategyEngine
                if symbol in self.all_indices and self.shm_global:
                    self.shm_global.write(signals)
            
            # 3. Publish over ZeroMQ [Audit 2.2: Fix parameter order]
            if hasattr(self, 'pub') and self.pub:
                await self.mq.send_json(self.pub, f"{Topics.MARKET_STATE}.{symbol}", market_signal_state)
            
            # Persist state by symbol
            await self._redis.set(f"latest_market_state:{symbol}", json.dumps(market_signal_state, cls=NumpyEncoder))
            if symbol == "NIFTY50":
                await self._redis.set("latest_market_state", json.dumps(market_signal_state, cls=NumpyEncoder))

            # [Audit-Fix] Walls are now managed by SnapshotManager (Slow Lane)
            # MarketSensor simply consumes them from Redis for the dashboard state

            # Persist history for UI charts
            await self._persist_signal_history(market_signal_state)
            
            # Publish individual signals for strategy guards (Partitioned by Asset)
            await self._redis.set(f"dispersion_coeff:{symbol}", str(market_signal_state["dispersion_coeff"]))
            await self._redis.set(f"log_ofi_zscore:{symbol}", str(market_signal_state["log_ofi_zscore"]))
            await self._redis.set(f"cvd_absorption:{symbol}", "1" if market_signal_state["cvd_absorption"] else "0")
            await self._redis.set(f"cvd_flip_ticks:{symbol}", str(market_signal_state["cvd_flip_ticks"]))
            await self._redis.set(f"price_dislocation:{symbol}", "1" if market_signal_state["price_dislocation"] else "0")
            await self._redis.set(f"gex_sign:{symbol}", market_signal_state["gex_sign"])
            await self._redis.set(f"atr:{symbol}", str(market_signal_state["atr"]))
            await self._redis.set(f"asto:{symbol}", str(market_signal_state["asto"]))
            await self._redis.set(f"asto_regime:{symbol}", str(market_signal_state["asto_regime"]))
            await self._redis.set(f"flow_toxicity_veto:{symbol}", "1" if market_signal_state["flow_toxicity_veto"] else "0")
            await self._redis.set(f"rsi:{symbol}", str(market_signal_state["rsi"]))
            await self._redis.set(f"pcr:{symbol}", str(market_signal_state["pcr"]))
            await self._redis.set(f"change_pct:{symbol}", str(market_signal_state["change_pct"]))
            await self._redis.set(f"iv_atm:{symbol}", str(market_signal_state["iv_atm"]))
            await self._redis.set(f"current_dte:{symbol}", str(market_signal_state.get("dte", 2)))
            
            # Legacy compatibility for NIFTY50
            if symbol == "NIFTY50":
                await self._redis.set("dispersion_coeff", str(market_signal_state["dispersion_coeff"]))
                await self._redis.set("log_ofi_zscore", str(market_signal_state["log_ofi_zscore"]))
                await self._redis.set("cvd_absorption", "1" if market_signal_state["cvd_absorption"] else "0")
                await self._redis.set("cvd_flip_ticks", str(market_signal_state["cvd_flip_ticks"]))
                await self._redis.set("gex_sign", market_signal_state["gex_sign"])
                await self._redis.set("atr", str(market_signal_state["atr"]))
                await self._redis.set("rv", str(market_signal_state["rv"]))
                await self._redis.set("asto", str(market_signal_state["asto"]))
                await self._redis.set("asto_regime", str(market_signal_state["asto_regime"]))
                await self._redis.set("current_dte", str(market_signal_state.get("dte", 2)))
            
            # COMPOSITE_ALPHA: partitioned and flat legacy
            await self._redis.set(f"COMPOSITE_ALPHA:{symbol}", str(market_signal_state["s_total"]))
            if symbol == "NIFTY50":
                await self._redis.set("COMPOSITE_ALPHA", str(market_signal_state["s_total"]))

            # HMM_REGIME: pull from partitioned regime hash [Audit 3.1: Standardize NIFTY50]
            hmm_raw = await self._redis.hget("hmm_regime_state", symbol)
            if hmm_raw:
                hmm_data = json.loads(hmm_raw)
                val = hmm_data.get("regime", "WAITING")
                await self._redis.set(f"HMM_REGIME:{symbol}", val)
                if symbol == "NIFTY50":
                    await self._redis.set("HMM_REGIME", val)
                    await self._redis.set("hmm_regime", val)
            else:
                await self._redis.set(f"HMM_REGIME:{symbol}", "WAITING")

        if not self.test_mode:
            # GAP FIX: Store individual heavyweight Z-scores for API / Power Five
            if symbol in TOP_10_HEAVYWEIGHTS:
                hw_z = market_signal_state.get("log_ofi_zscore", 0.0)
                # Store in a dedicated hash for the API
                await self._redis.hset("power_five_matrix", symbol, json.dumps({
                    "price": price,
                    "z_score": round(hw_z, 2),
                    "timestamp": market_signal_state["timestamp"]
                }, cls=NumpyEncoder))

    async def _persist_signal_history(self, state: dict):
        """Pushes the current state into a rolling Redis list for UI history."""
        try:
            # Save a stripped down version for the history charts
            history_item = {
                "timestamp": state["timestamp"],
                "s_total": state["s_total"],
                "log_ofi_zscore": state["log_ofi_zscore"],
                "cvd_absorption": state["cvd_absorption"],
                "vpin": state["vpin"],
                "vanna": state["vanna"],
                "charm": state["charm"]
            }
            await self._redis.lpush("signal_history", json.dumps(history_item, cls=NumpyEncoder))
            await self._redis.ltrim("signal_history", 0, 3600) # Keep 1 hour of history
        except Exception as e:
            logger.error(f"Failed to persist signal history: {e}")


if __name__ == "__main__":
    # Required for multiprocessing on Windows
    mp.set_start_method("spawn", force=True)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    sensor = MarketSensor()
    asyncio.run(sensor.start())
