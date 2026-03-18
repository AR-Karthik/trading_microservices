"""
daemons/market_sensor.py
========================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Architecture:
  Main asyncio loop  →  ZMQ I/O + Polars feature prep
  ComputeProcess     →  Heavy math (GEX, dispersion, Greeks) 
  IPC               →  multiprocessing.Queue

Signals computed for NIFTY, BANKNIFTY, SENSEX:
  - Log-OFI Z-score
  - Zero-Gamma Level
  - Index Dispersion
  - Volatility Term Structure
  - Vanna & Charm
  - VPIN
  - CVD Absorption
  - Composite Alpha Score
"""

import asyncio
import collections
import json
import logging
import math
import multiprocessing as mp
import sys
import time
import queue
import re
from datetime import datetime, timezone
from typing import Any  # [R2-20] Removed duplicate 'import sys'

import numpy as np # type: ignore
import polars as pl # type: ignore
import redis.asyncio as redis # type: ignore

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

# [Audit-Fix] Asset-specific components for Alpha Calculation
INDEX_COMPONENTS = {
    "NIFTY50": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT"],
    "BANKNIFTY": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "AUBL", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK"],
    "SENSEX": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "TCS", "LT", "AXISBANK", "SBIN", "KOTAKBANK"]
}
OFI_WINDOW = 100          # ticks for rolling OFI
DISPERSION_WINDOW_MIN = 3 # minutes for correlation rolling window
# RISK_FREE_RATE now dynamic (Audit 5.1)
NEAR_TERM_DTE = 2         # days for "near term" IV
FAR_TERM_DTE = 30         # days for "far term" IV

# ── Signal Math Helpers (SRS §2.3) ───────────────────────────────────────────

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

        # ── ATR (20-tick) ──────────────────────────────────────────────────
        prices = np.array(snapshot.get("price_series", [spot]))
        if len(prices) >= 2:
            tr = np.abs(np.diff(prices))
            result["atr"] = float(np.mean(tr[-20:]))
        else:
            result["atr"] = 20.0

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
        
        # ADX requires H/L/C - if not provided, we proxy with close-only simplified version
        # In this TBT stream, we treat each tick as C, and simulate H/L if needed
        result["adx"] = calculate_adx(prices, prices*0.999, prices, 14) 

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
            
        # Get High/Low from price series in snapshot for precise ASTO
        prices = np.array(snapshot.get("price_series", [spot]))
        # We use the most recent 10 ticks (ATR-10 context) to find local H/L
        window_prices = prices[-10:] if len(prices) >= 10 else prices
        curr_high = float(np.max(window_prices))
        curr_low = float(np.min(window_prices))

        asto_raw, asto_regime, m_adaptive = engine.compute(curr_high, curr_low, spot, z_vol)
        result["asto"] = float(asto_raw)
        result["asto_regime"] = int(asto_regime)
        result["asto_multiplier"] = float(m_adaptive)


        return result

    # Worker main loop
    while True:
        try:
            snapshot = in_queue.get(timeout=1.0)
            if snapshot is None:
                break  # Sentinel → shutdown
            signals = compute_signals(snapshot)
            out_queue.put(signals)
        except mp.queues.Empty:
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
        self.mq = MQManager()
        self.test_mode = test_mode
        self.scorer = CompositeAlphaScorer()

        redis_host = os.getenv("REDIS_HOST", "localhost")
        if not test_mode:
            self.pub = self.mq.create_publisher(Ports.MARKET_STATE)
            self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
            self.shm = ShmManager(mode='w')
        else:
            self.shm = None
            self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)

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
            idx: collections.deque(maxlen=200) for idx in self.all_indices
        }
        self.vix_series: collections.deque[float] = collections.deque(maxlen=300) # 5m @ 1s pub - Keep global as VIX is cross-asset
        self.cvd: dict[str, float] = collections.defaultdict(float)
        self.cvd_series: dict[str, collections.deque[float]] = {
            idx: collections.deque(maxlen=200) for idx in self.all_indices
        }
        self.basis_series: dict[str, collections.deque[float]] = {
            idx: collections.deque(maxlen=500) for idx in self.all_indices
        }
        self.spot_15m_series: dict[str, collections.deque[float]] = {
            idx: collections.deque(maxlen=900) for idx in self.all_indices
        }

        # VPIN State (SRS Phase 2) - Asset Scoped
        self.vpin_bucket_size = 5000  # [D-01] Spec: 5000 volume units per bucket, was 100
        self.vpin_current_vol: dict[str, int] = collections.defaultdict(int)
        self.vpin_buy_vol: dict[str, int] = collections.defaultdict(int)
        self.vpin_sell_vol: dict[str, int] = collections.defaultdict(int)
        self.vpin_series: dict[str, collections.deque[float]] = {
            idx: collections.deque(maxlen=50) for idx in self.all_indices
        }

        # ASTO Indicators (SRS Part 1)
        self.atr_buffer: collections.deque[float] = collections.deque(maxlen=10)
        self.asto_engine = AdaptiveSuperTrendOscillator(base_multiplier=3.0, phi=0.5)

        # Compute subprocess setup
        self._compute_in: mp.Queue = mp.Queue(maxsize=50)
        self._compute_out: mp.Queue = mp.Queue(maxsize=50)
        self._latest_signals: dict[str, Any] = {}
        self._compute_proc: mp.Process | None = None

        # Hurst exponent cache
        self._last_hurst_calc = 0.0

        # [Hedge Hybrid] Holistic state
        self.market_state = "NEUTRAL"
        self.vwap_cum_pv: dict[str, float] = {}
        self.vwap_cum_vol: dict[str, float] = {}

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
            self.hb = HeartbeatProvider("MarketSensor", self.mq)
            await self.hb.start()

            self.main_task = asyncio.create_task(self.run())
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
            asyncio.create_task(self.hb.stop())
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
            sign = 0.0     # Neutral / Mid-quote bounce

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
        """Order Flow Imbalance (OFI) calculation."""
        # Simplified OFI: (bid_size - ask_size) * price_change_direction
        # A more robust OFI would involve tracking order book changes
        bid_size = tick.get("bid_size", 0)
        ask_size = tick.get("ask_size", 0)
        last_price = tick.get("price", 0.0)
        prev_price = self.tick_store[tick.get("symbol", "NIFTY50")][-1].get("price", last_price) if self.tick_store[tick.get("symbol", "NIFTY50")] else last_price

        price_change_direction = 0
        if last_price > prev_price:
            price_change_direction = 1
        elif last_price < prev_price:
            price_change_direction = -1

        return (bid_size - ask_size) * price_change_direction

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
            
        # When bucket fills, calculate VPIN and reset
        if self.vpin_current_vol[symbol] >= self.vpin_bucket_size:
            vpin_val = abs(self.vpin_buy_vol[symbol] - self.vpin_sell_vol[symbol]) / self.vpin_current_vol[symbol]
            self.vpin_series[symbol].append(vpin_val)
            self.vpin_current_vol[symbol] = 0
            self.vpin_buy_vol[symbol] = 0
            self.vpin_sell_vol[symbol] = 0

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

    async def _initialize_redis_state(self):
        """Pre-seeds Redis with UNKNOWN state on boot to avoid nil errors."""
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

    async def run(self):
        # Initialize Redis state for each symbol on boot
        # This prevents (nil) errors in the dashboard before the first 50 ticks arrive.
        await self._initialize_redis_state()

        if not self.test_mode:
            # The _start_compute_process is now handled by the new `start` method
            pass

        # ── Phase 9: UI & Observability heartbeat ──
        from core.health import HeartbeatProvider
        self.hb = HeartbeatProvider("MarketSensor", self._redis)
        asyncio.create_task(self.hb.run_heartbeat())

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
                    price = tick.get("price", 0.0)

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

                    # Simulate basis (futures_price - spot)
                    futures_est = price * (1 + 0.0005 * (np.random.random() - 0.5))
                    self.basis_series[symbol].append(futures_est - price)

                    # [Audit Fix] 15m Spot series for all indices
                    if symbol in self.all_indices:
                        self.spot_15m_series[symbol].append(float(price))

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
                            "ofi_series": list(self.ofi_series[symbol]),
                            "hw_prices": {k: list(v) for k, v in self.hw_prices.items() if len(v) >= 10},
                            "cvd_series": list(self.cvd_series[symbol]),
                            "basis_series": list(self.basis_series[symbol]),
                            "spot_15m_series": list(self.spot_15m_series[symbol]),
                            "vpin_series": list(self.vpin_series[symbol]) if self.vpin_series[symbol] else [0.0],
                            "zero_gamma_level": find_zero_gamma_level(np.array(price_series), price),
                            "price_series": price_series,
                            "hurst_val": calculate_hurst(np.array(price_series[-500:])) if len(price_series) >= 50 else 0.5,
                            "er_window": 10, # Dynamic via Firestore later
                            "strikes": [price - 200, price - 100, price, price + 100, price + 200],
                            "near_term_iv": await self._redis.get("atm_iv") or 0.20,
                            "far_term_iv": 0.17,
                            "atm_iv": await self._redis.get("atm_iv") or 0.18,
                            "dte": 2,
                            "sentiment_score": float(await self._redis.get("news_sentiment_score") or 0.0),
                            "risk_free_rate": float(await self._redis.get("CONFIG:RISK_FREE_RATE") or 0.065)
                        }
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

                except Exception as e:
                    logger.error(f"Market Sensor Data Sync Error: {e}")
                    await asyncio.sleep(0.1)

        finally:
            # The _stop_compute_process is now handled by the new `stop` method
            sub.close()

    async def _publish_market_state(self, symbol: str, price: float):
        """Assembles and publishes the full market state vector."""
        sig = self._latest_signals

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

        state = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "s_total": s_total,
            "s_env": s_env,
            "s_str": s_str,
            "s_div": s_div,
            "hurst": sig.get("hurst", 0.5),
            "kaufman_er": sig.get("kaufman_er", 0.5),
            "adx": sig.get("adx", 20.0),
            "rv": float(np.std(np.diff(np.log(np.maximum(prices_arr, 1e-6))))) if len(prices_arr) >= 2 else 0.0,
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
            "time_of_day": datetime.now().strftime("%H:%M:%S")
        }

        # ── [Hedge Hybrid] Three-State Deterministic Model (S22 & MARKET_STATE) ──
        asto = float(state.get("asto", 0.0))
        abs_asto = abs(asto)
        
        # 1. Update Anchored VWAP (resets at sensor start)
        tick = self.tick_store[symbol][-1] if self.tick_store[symbol] else {}
        vol = float(tick.get("last_volume", 1))
        
        self.vwap_cum_pv[symbol] = self.vwap_cum_pv.get(symbol, 0.0) + (price * vol)
        self.vwap_cum_vol[symbol] = self.vwap_cum_vol.get(symbol, 0.0) + vol
        
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
        state["whale_pivot"] = s22

        # 4. Update MARKET_STATE with Hysteresis - Asset Scoped [Audit Fix]
        current_state = str(await self._redis.get(f"MARKET_STATE:{symbol}") or "NEUTRAL")
        new_state = current_state
        
        if abs_asto >= 90:
            new_state = "EXTREME_TREND:BULLISH" if asto >= 90 else "EXTREME_TREND:BEARISH"
        elif abs_asto >= 70:
            if "EXTREME" not in current_state:
                new_state = "TRENDING"
        else: # abs_asto < 70
            new_state = "NEUTRAL"
            
        state["market_state"] = new_state
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
            # For simplicity, we assume 'entry' is tracked in session or we use a baseline
            atr = float(state.get("atr", 20.0))
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
            
            state["exit_path_70_30"] = {
                "tp1": round(float(tp1), 2),
                "tp2": round(float(tp2), 2),
                "progress": round(float(progress), 2)
            }

            if self.shm:
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
                    s_total=state["s_total"],
                    vpin=state["vpin"],
                    ofi_z=state["log_ofi_zscore"],
                    vanna=state.get("vanna", 0.0),
                    charm=state.get("charm", 0.0),
                    s_env=state.get("s_env", 0.0),
                    s_str=state.get("s_str", 0.0),
                    s_div=state.get("s_div", 0.0),
                    rv=state["rv"],
                    adx=state["adx"],
                    pcr=state.get("pcr", curr_pcr),
                    asto=state["asto"],
                    asto_regime=state["asto_regime"],
                    whale_pivot=state["whale_pivot"],
                    net_delta_nifty=nd_nifty,
                    net_delta_banknifty=nd_bank,
                    net_delta_sensex=nd_sensex,
                    veto=bool(state.get("flow_toxicity_veto", False)),
                    hw_alpha=hw_list
                )
                self.shm.write(signals)
            
            # 3. Publish over ZeroMQ [Audit 2.2: Fix parameter order]
            await self.mq.send_json(self.pub, Topics.MARKET_STATE, state)
            
            # Persist state by symbol
            await self._redis.set(f"latest_market_state:{symbol}", json.dumps(state, cls=NumpyEncoder))
            if symbol == "NIFTY50":
                await self._redis.set("latest_market_state", json.dumps(state, cls=NumpyEncoder))

            # Persist history for UI charts
            await self._persist_signal_history(state)
            
            # Publish individual signals for strategy guards (Partitioned by Asset)
            await self._redis.set(f"dispersion_coeff:{symbol}", str(state["dispersion_coeff"]))
            await self._redis.set(f"log_ofi_zscore:{symbol}", str(state["log_ofi_zscore"]))
            await self._redis.set(f"cvd_absorption:{symbol}", "1" if state["cvd_absorption"] else "0")
            await self._redis.set(f"cvd_flip_ticks:{symbol}", str(state["cvd_flip_ticks"]))
            await self._redis.set(f"price_dislocation:{symbol}", "1" if state["price_dislocation"] else "0")
            await self._redis.set(f"gex_sign:{symbol}", state["gex_sign"])
            await self._redis.set(f"atr:{symbol}", str(state["atr"]))
            await self._redis.set(f"asto:{symbol}", str(state["asto"]))
            await self._redis.set(f"asto_regime:{symbol}", str(state["asto_regime"]))
            await self._redis.set(f"flow_toxicity_veto:{symbol}", "1" if state["flow_toxicity_veto"] else "0")
            await self._redis.set(f"rsi:{symbol}", str(state["rsi"]))
            await self._redis.set(f"pcr:{symbol}", str(state["pcr"]))
            await self._redis.set(f"change_pct:{symbol}", str(state["change_pct"]))
            await self._redis.set(f"current_dte:{symbol}", str(state.get("dte", 2)))
            
            # Legacy compatibility for NIFTY50
            if symbol == "NIFTY50":
                await self._redis.set("dispersion_coeff", str(state["dispersion_coeff"]))
                await self._redis.set("log_ofi_zscore", str(state["log_ofi_zscore"]))
                await self._redis.set("cvd_absorption", "1" if state["cvd_absorption"] else "0")
                await self._redis.set("cvd_flip_ticks", str(state["cvd_flip_ticks"]))
                await self._redis.set("gex_sign", state["gex_sign"])
                await self._redis.set("atr", str(state["atr"]))
                await self._redis.set("rv", str(state["rv"]))
                await self._redis.set("asto", str(state["asto"]))
                await self._redis.set("asto_regime", str(state["asto_regime"]))
                await self._redis.set("current_dte", str(state.get("dte", 2)))
            
            # COMPOSITE_ALPHA: partitioned and flat legacy
            await self._redis.set(f"COMPOSITE_ALPHA:{symbol}", str(state["s_total"]))
            if symbol == "NIFTY50":
                await self._redis.set("COMPOSITE_ALPHA", str(state["s_total"]))

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
            # ... existing publication logic ...
            
            # GAP FIX: Store individual heavyweight Z-scores for API / Power Five
            if symbol in TOP_10_HEAVYWEIGHTS:
                hw_z = state.get("log_ofi_zscore", 0.0)
                # Store in a dedicated hash for the API
                await self._redis.hset("power_five_matrix", symbol, json.dumps({
                    "price": price,
                    "z_score": round(hw_z, 2),
                    "timestamp": state["timestamp"]
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
    asyncio.run(sensor.run())
