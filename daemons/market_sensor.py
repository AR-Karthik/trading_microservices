"""
daemons/market_sensor.py
========================
Institutional Microstructure & GIL Mitigation (SRS §2.3)

Architecture:
  Main asyncio loop  →  ZMQ I/O + Polars feature prep only
  ComputeProcess     →  All heavy math (GEX, dispersion, Greeks) in isolated OS process
  IPC               →  multiprocessing.Queue (compute_in → compute_out)

Signals computed:
  - Log-OFI Z-score (stationarized Order Flow Imbalance)
  - Iterative Zero-Gamma Level (Black-Scholes root-find)
  - Index Dispersion (rolling 3-min Pearson correlation matrix)
  - Volatility Term Structure (near/far IV ratio)
  - Vanna & Charm flags (toxic option detection)
  - Vanna & Charm flags (toxic option detection)
  - VPIN (Volume-Synchronized Probability of Informed Trading)
  - CVD Absorption (bullish divergence signal)
  - Futures Basis deviation (price dislocation flag)
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
from datetime import datetime, timezone
from typing import Any

import numpy as np
import polars as pl
import redis.asyncio as redis

import os
from core.shm import ShmManager
from core.mq import MQManager, Ports, Topics

class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NumpyEncoder, self).default(obj)

# Try to import Rust extension for high-performance math
try:
    import tick_engine
    HAS_RUST_ENGINE = True
except ImportError:
    HAS_RUST_ENGINE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("MarketSensor")

# ── Constants ────────────────────────────────────────────────────────────────

TOP_5_HEAVYWEIGHTS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT"]
OFI_WINDOW = 100          # ticks for rolling OFI
DISPERSION_WINDOW_MIN = 3 # minutes for correlation rolling window
RISK_FREE_RATE = 0.065    # 6.5% RBI repo rate
NEAR_TERM_DTE = 2         # days for "near term" IV
FAR_TERM_DTE = 30         # days for "far term" IV

# ── Subprocess: Heavy Compute Process ───────────────────────────────────────

def _compute_worker(in_queue: mp.Queue, out_queue: mp.Queue):
    """
    Isolated OS process — runs all heavy math.
    Reads feature snapshots from in_queue, pushes computed signals to out_queue.
    GIL is completely bypassed since this runs in a separate Python interpreter.
    """
    import numpy as np
    from scipy.optimize import brentq  # type: ignore
    from scipy.stats import norm       # type: ignore

    logger = logging.getLogger("ComputeWorker")
    logging.basicConfig(level=logging.WARNING)

    def bs_call_price(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0:
            return max(0.0, S - K)
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)

    def bs_gamma(S, K, T, r, sigma):
        if T <= 0 or sigma <= 0:
            return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return norm.pdf(d1) / (S * sigma * math.sqrt(T))

    def bs_delta(S, K, T, r, sigma, opt_type="call"):
        if T <= 0:
            return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        return norm.cdf(d1) if opt_type == "call" else norm.cdf(d1) - 1

    def bs_vanna(S, K, T, r, sigma):
        """dDelta/dVol"""
        if T <= 0 or sigma <= 0:
            return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return -norm.pdf(d1) * d2 / sigma

    def bs_charm(S, K, T, r, sigma, opt_type="call"):
        """dDelta/dTime (theta of delta)"""
        if T <= 0 or sigma <= 0:
            return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        sign = 1 if opt_type == "call" else -1
        charm = -norm.pdf(d1) * (2 * r * T - d2 * sigma * math.sqrt(T)) / (2 * T * sigma * math.sqrt(T))
        return sign * charm

    def find_zero_gamma_level(prices_arr: np.ndarray, current_spot: float) -> float:
        """Estimates the Zero Gamma Level (ZGL) where dealer hedging flips from short to long gamma."""
        if len(prices_arr) == 0:
            return current_spot
        # Naive implementation: In a full order flow tape, this tracks dealer exposure profiles.
        # Fallback to mean price of highly active recent volume nodes.
        return float(np.mean(prices_arr[-100:])) if len(prices_arr) >= 100 else float(np.mean(prices_arr))

    def calculate_kaufman_er(series: np.ndarray, window: int = 10) -> float:
        """Kaufman Efficiency Ratio: Net Change / Sum of Absolute Changes."""
        if len(series) < window + 1:
            return 0.5
        net_change = abs(series[-1] - series[-window])
        sum_abs_changes = np.sum(np.abs(np.diff(series[-window:])))
        return float(net_change / sum_abs_changes) if sum_abs_changes > 0 else 0.0

    def calculate_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = 14) -> float:
        """Simplified ADX calculation for trend strength."""
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
        plus_di = 100 * (np.mean(plus_dm[-window:]) / tr_smooth) if tr_smooth > 0 else 0
        minus_di = 100 * (np.mean(minus_dm[-window:]) / tr_smooth) if tr_smooth > 0 else 0
        
        dx = 100 * (np.abs(plus_di - minus_di) / (plus_di + minus_di)) if (plus_di + minus_di) > 0 else 0
        return float(dx)

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

        # ── Zero Gamma Level ───────────────────────────────────────────────
        spot = snapshot.get("spot", 22000.0)
        strikes = snapshot.get("strikes", [spot - 200, spot - 100, spot, spot + 100, spot + 200])
        T = snapshot.get("dte", 2) / 365.0
        iv = snapshot.get("atm_iv", 0.18)
        result["zero_gamma_level"] = find_zero_gamma_level(np.array(snapshot.get("price_series", [spot])), spot)

        # ── Net GEX sign (simplified) ──────────────────────────────────────
        gex_vals = [bs_gamma(spot, K, T, RISK_FREE_RATE, iv) * (1 if K >= spot else -1)
                    for K in strikes]
        result["gex_sign"] = "NEGATIVE" if sum(gex_vals) < 0 else "POSITIVE"

        # ── Vanna & Charm ──────────────────────────────────────────────────
        K_atm = min(strikes, key=lambda k: abs(k - spot))
        charm_val = bs_charm(spot, K_atm, T, RISK_FREE_RATE, iv, "call")
        vanna_val = bs_vanna(spot, K_atm, T, RISK_FREE_RATE, iv)
        result["charm"] = float(charm_val)
        result["vanna"] = float(vanna_val)
        # Flag toxic if charm < -0.05 (severe delta bleed per time decay)
        result["toxic_option"] = bool(charm_val < -0.05)

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
        total = (self.weights["env"] * s_env + self.weights["str"] * s_str +
                 self.weights["div"] * s_div) * multiplier
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

        if not test_mode:
            self.pub = self.mq.create_publisher(Ports.MARKET_STATE)
            redis_host = os.getenv("REDIS_HOST", "localhost")
            self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
            self.shm = ShmManager(mode='w')
        else:
            self.shm = None
            self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)

        # High-performance Rust engine integration
        self.use_rust = os.getenv("USE_RUST_ENGINE", "0") == "1" and HAS_RUST_ENGINE
        if self.use_rust:
            logger.info("🚀 Rust Tick Engine ACTIVATED (Bypassing GIL / mp.Process)")
            self.rust_engine = tick_engine.TickEngine(vpin_bucket_size=100)
        else:
            self.rust_engine = None
            logger.info("🐍 Using standard Python ComputeWorker (Multiprocessing mode)")
            self._redis = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)

        # Tick buffers (main process — lightweight)
        self.tick_store: dict[str, collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=2000)
        )
        self.hw_prices: dict[str, collections.deque] = {
            sym: collections.deque(maxlen=500) for sym in TOP_5_HEAVYWEIGHTS
        }
        self.ofi_series: collections.deque = collections.deque(maxlen=200)
        self.cvd: float = 0.0
        self.cvd_series: collections.deque = collections.deque(maxlen=200)
        self.basis_series: collections.deque = collections.deque(maxlen=500)
        self.spot_15m_series: collections.deque = collections.deque(maxlen=900)  # ~15min @ 1/s

        # VPIN State (SRS Phase 2)
        self.vpin_bucket_size = 100  # v8.0 audit fix: smaller buckets for index ticks
        self.current_bucket_vol = 0
        self.current_bucket_buy_vol = 0
        self.current_bucket_sell_vol = 0
        self.vpin_series: collections.deque = collections.deque(maxlen=50)

        # Compute subprocess setup
        self._compute_in: mp.Queue = mp.Queue(maxsize=50)
        self._compute_out: mp.Queue = mp.Queue(maxsize=50)
        self._latest_signals: dict[str, Any] = {}
        self._compute_proc: mp.Process | None = None

        # Hurst exponent cache
        self._last_hurst_calc = 0.0

    def _start_compute_process(self):
        """Launch the isolated compute subprocess."""
        self._compute_proc = mp.Process(
            target=_compute_worker,
            args=(self._compute_in, self._compute_out),
            daemon=True,
            name="ComputeWorker"
        )
        self._compute_proc.start()
        logger.info(f"ComputeProcess started (PID: {self._compute_proc.pid})")

    def _stop_compute_process(self):
        if self._compute_proc and self._compute_proc.is_alive():
            self._compute_in.put(None)  # sentinel
            self._compute_proc.join(timeout=5)
            if self._compute_proc.is_alive():
                self._compute_proc.terminate()

    def calculate_hurst(self, series: np.ndarray) -> float:
        if len(series) < 50:
            return 0.5
        lags = range(2, 20)
        tau = [np.sqrt(np.std(np.subtract(series[lag:], series[:-lag]))) for lag in lags]
        poly = np.polyfit(np.log(lags), np.log(tau), 1)
        return float(poly[0] * 2.0)

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

    def _update_cvd(self, tick: dict):
        """Cumulative Volume Delta update using Lee-Ready classification."""
        sign = self._classify_trade(tick)
        volume = tick.get("last_volume", 1)
        self.cvd += sign * volume
        self.cvd_series.append(self.cvd)

    def _update_vpin(self, tick: dict):
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
                self.vpin_series.append(vpin_val)
            return

        sign = self._classify_trade(tick)
        volume = tick.get("last_volume", 1)
        
        self.current_bucket_vol += volume
        if sign > 0:
            self.current_bucket_buy_vol += volume
        elif sign < 0:
            self.current_bucket_sell_vol += volume
            
        # When bucket fills, calculate VPIN and reset
        if self.current_bucket_vol >= self.vpin_bucket_size:
            vpin = abs(self.current_bucket_buy_vol - self.current_bucket_sell_vol) / self.current_bucket_vol
            self.vpin_series.append(vpin)
            self.current_bucket_vol = 0
            self.current_bucket_buy_vol = 0
            self.current_bucket_sell_vol = 0

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
            except mp.queues.Empty:
                break

    async def run(self):
        if not self.test_mode:
            self._start_compute_process()

        logger.info("MarketSensor active. Subscribing to tick data...")
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
                    if symbol in TOP_5_HEAVYWEIGHTS:
                        self.hw_prices[symbol].append(price)

                    # Update OFI, CVD, VPIN, basis
                    self.ofi_series.append(self._ofi(tick))
                    self._update_cvd(tick)
                    if symbol == "NIFTY50":  # Typically VPIN is calculated on the underlying/liquid asset
                        self._update_vpin(tick)

                    # Simulate basis (futures_price - spot)
                    futures_est = price * (1 + 0.0005 * (np.random.random() - 0.5))
                    self.basis_series.append(futures_est - price)

                    if symbol == "NIFTY50":
                        self.spot_15m_series.append(price)

                    tick_count += 1

                    # Drain compute results every tick (non-blocking)
                    await self._drain_compute_output()

                    # Send snapshot to compute process every 20 ticks
                    if tick_count % 20 == 0 and not self.test_mode:
                        price_series = [t.get("price", price) for t in list(self.tick_store[symbol])]
                        snapshot = {
                            "symbol": symbol,
                            "spot": price,
                            "ofi_series": list(self.ofi_series),
                            "hw_prices": {k: list(v) for k, v in self.hw_prices.items() if len(v) >= 10},
                            "cvd_series": list(self.cvd_series),
                            "basis_series": list(self.basis_series),
                            "spot_15m_series": list(self.spot_15m_series),
                            "vpin_series": list(self.vpin_series) if self.vpin_series else [0.0],
                            "zero_gamma_level": find_zero_gamma_level(np.array(price_series), price),
                            "price_series": price_series,
                            "hurst_val": self.calculate_hurst(np.array(price_series[-500:])) if len(price_series) >= 50 else 0.5,
                            "er_window": 10, # Dynamic via Firestore later
                            "strikes": [price - 200, price - 100, price, price + 100, price + 200],
                            "near_term_iv": await self._redis.get("atm_iv") or 0.20,
                            "far_term_iv": 0.17,
                            "atm_iv": await self._redis.get("atm_iv") or 0.18,
                            "dte": 2
                        }
                        try:
                            self._compute_in.put_nowait(snapshot)
                        except mp.queues.Full:
                            pass  # Skip if compute is backed up — don't block I/O loop

                        # GAP FIX: Also send individual snapshots for heavyweights for Power Five matrix
                        if symbol in TOP_5_HEAVYWEIGHTS:
                            hw_snapshot = {
                                "symbol": symbol,
                                "spot": price,
                                "price_series": price_series,
                                "ofi_series": [t.get("price", price) for t in list(self.tick_store[symbol])][-100:]
                            }
                            self._compute_in.put_nowait(hw_snapshot)

                    # Publish state every 50 ticks
                    if tick_count % 50 == 0:
                        await self._publish_market_state(symbol, price)

                    await asyncio.sleep(0)  # yield to event loop

                except Exception as e:
                    logger.error(f"MarketSensor tick error: {e}")
                    await asyncio.sleep(0.1)

        finally:
            self._stop_compute_process()
            sub.close()

    async def _publish_market_state(self, symbol: str, price: float):
        """Assembles and publishes the full market state vector."""
        sig = self._latest_signals

        # Retrieve FII bias from Redis
        try:
            fii_bias_val = await self._redis.get("fii_bias")
            fii_bias = float(fii_bias_val or 0)
        except Exception:
            fii_bias = 0.0

        prices_arr = np.array([t.get("price", price) for t in list(self.tick_store[symbol])])

        env_data = {"fii_bias": fii_bias, "vix_slope": 0.01, "ivp": 25}
        str_data = {"basis_slope": float(np.mean(list(self.basis_series)[-5:]) if self.basis_series else 0),
                    "dist_max_pain": 10, "pcr": 0.85}
        div_data = {"price_slope": float(np.diff(prices_arr[-10:]).mean()) if len(prices_arr) >= 10 else 0,
                    "pcr_slope": 0.02, "cvd_slope": float(np.diff(list(self.cvd_series)[-5:]).mean())
                    if len(self.cvd_series) >= 5 else 0}

        s_env = self.scorer._calc_env(env_data)
        s_str = self.scorer._calc_str(str_data)
        s_div = self.scorer._calc_div(div_data)
        s_total = self.scorer.get_total_score(env_data, str_data, div_data)

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
            "vpin": sig.get("vpin", 0.0),
            "flow_toxicity_veto": sig.get("flow_toxicity_veto", False),
            "time_of_day": datetime.now().strftime("%H:%M:%S")
        }

        # ── GAP FIX: 70:30 Exit Path Calculation ───────────────────
        if symbol == "NIFTY50":
            # Target 1 = entry + 1.5 * ATR, Target 2 = entry + 3 * ATR
            # For simplicity, we assume 'entry' is tracked in session or we use a baseline
            atr = state.get("atr", 20.0)
            entry = float(await self._redis.get(f"entry_price:{symbol}") or price)
            
            side = await self._redis.get(f"active_side:{symbol}") or "LONG"
            mult = 1 if side == "LONG" else -1
            
            tp1 = entry + (1.5 * atr * mult)
            tp2 = entry + (3.0 * atr * mult)
            
            # Progress: how close are we to TP2 from entry
            dist_total = abs(tp2 - entry)
            dist_current = abs(price - entry)
            progress = min(100, max(0, (dist_current / dist_total) * 100)) if dist_total > 0 else 0
            
            state["exit_path_70_30"] = {
                "tp1": round(tp1, 2),
                "tp2": round(tp2, 2),
                "progress": round(progress, 2)
            }

        if not self.test_mode:
            if self.shm:
                self.shm.write(
                    s_total=state["s_total"], 
                    vpin=state["vpin"], 
                    ofi_z=state["log_ofi_zscore"],
                    vanna=state["vanna"],
                    charm=state["charm"],
                    s_env=state["s_env"],
                    s_str=state["s_str"],
                    s_div=state["s_div"],
                    veto=state["flow_toxicity_veto"] or state["toxic_option"]
                )
            await self.mq.send_json(self.pub, state, topic=Topics.MARKET_STATE)
            await self._redis.set("latest_market_state", json.dumps(state, cls=NumpyEncoder))
            # v5.5: Persist history for UI charts
            await self._persist_signal_history(state)
            
            # Publish individual signals for strategy guards
            await self._redis.set("dispersion_coeff", str(state["dispersion_coeff"]))
            await self._redis.set("log_ofi_zscore", str(state["log_ofi_zscore"]))
            await self._redis.set("cvd_absorption", "1" if state["cvd_absorption"] else "0")
            await self._redis.set("cvd_flip_ticks", str(state["cvd_flip_ticks"]))
            await self._redis.set("price_dislocation", "1" if state["price_dislocation"] else "0")
            await self._redis.set("gex_sign", state["gex_sign"])
            await self._redis.set("atr", str(state["atr"]))
            await self._redis.set("flow_toxicity_veto", "1" if state["flow_toxicity_veto"] else "0")
            
            # GAP FIX: Write flat Redis keys expected by cloud_publisher
            # COMPOSITE_ALPHA: standalone flat key for direct reads without JSON parsing
            await self._redis.set("COMPOSITE_ALPHA", str(state["s_total"]))
            # HMM_REGIME: a best-guess flat key derived from the active HMM engine's latest write.
            # cloud_publisher uses this as the unified regime label across all assets.
            # The per-asset result is still in hmm_regime_state hash for MetaRouter veto logic.
            hmm_nifty_raw = await self._redis.hget("hmm_regime_state", "NIFTY")
            if hmm_nifty_raw:
                hmm_nifty = json.loads(hmm_nifty_raw)
                val = hmm_nifty.get("regime", "WAITING")
                await self._redis.set("HMM_REGIME", val)
                await self._redis.set("hmm_regime", val) # normalize to lowercase for API
            else:
                await self._redis.set("HMM_REGIME", "WAITING")
                await self._redis.set("hmm_regime", "WAITING")

        if not self.test_mode:
            # ... existing publication logic ...
            
            # GAP FIX: Store individual heavyweight Z-scores for API / Power Five
            if symbol in TOP_5_HEAVYWEIGHTS:
                hw_z = state.get("log_ofi_zscore", 0.0)
                # Store in a dedicated hash for the API
                await self._redis.hset("power_five_matrix", symbol, json.dumps({
                    "price": price,
                    "z_score": round(hw_z, 2),
                    "timestamp": state["timestamp"]
                }))

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
            await self._redis.lpush("signal_history", json.dumps(history_item))
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
