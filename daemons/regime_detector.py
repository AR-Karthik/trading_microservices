"""
Market Regime Deterministic Engine
Determines market state (Trending, Ranging, Volatile) using ADX, RV, and IV.
Informs the Meta-Router on strategy selection and risk parameters.
"""

import argparse
import asyncio
import json
import logging
import os
import zmq
import zmq.asyncio
import sys
import time
from datetime import datetime
from collections import deque
import numpy as np
import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics, NumpyEncoder
from core.shm import ShmManager, SignalVector, RegimeShm, RegimeVector

# External API fallback for historic market data
try:
    from NorenRestApiPy.NorenApi import NorenApi
    import pyotp
    _HAS_SHOONYA = True
except ImportError:
    _HAS_SHOONYA = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("RegimeDetector")

# Regime Verdict constants
REGIME_RANGING = "RANGING"
REGIME_TRENDING = "TRENDING"
REGIME_HIGH_VOL_CHOP = "HIGH_VOL_CHOP"
REGIME_CRASH = "CRASH"
REGIME_TOXIC = "TOXIC"
REGIME_OVERBOUGHT = "OVERBOUGHT"
REGIME_OVERSOLD = "OVERSOLD"

# Maximum acceptable delay for realtime price ticks before triggering failsafe
STALE_DATA_THRESHOLD_S = 10

class RegimeDetector:
    def __init__(self, asset_id: str, core_pin: int):
        self.asset_id = asset_id
        self.core_pin = core_pin
        self.mq = MQManager()
        # Interaction Layer: Read signals and write regimes via Shared Memory
        self.shm_regime = RegimeShm(asset_id=asset_id, mode='w')
        self.shm_signals = ShmManager(asset_id=asset_id, mode='r')
        
        # Redis connection
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_pass = os.getenv("REDIS_PASSWORD", "")
        self.r = redis.Redis(host=redis_host, port=6379, db=0, password=redis_pass, decode_responses=True)
        
        # Regime State
        self.history_14d = [] # Fixed daily closes
        self.last_pcr = 1.0
        self.pcr_roc = 0.0
        self.adx_val = 0.0
        self.rv_val = 0.0
        self.iv_rv_spread = 0.0
        self.iv_val = 0.15       # Implied Volatility (ATM)
        self.vpin_val = 0.0      # Order Flow Toxicity
        self.ofi_z = 0.0         # Order Flow Imbalance (Z-score)
        self.day_high = 0.0      # Intraday Session High
        self.day_low = 0.0       # Intraday Session Low
        self.stale_override = False  # Failsafe threshold flag for stale data
        self.current_regime_int = 0  # 0:NEUTRAL, 1:TRENDING, 2:RANGING, 3:VOLATILE
        
        # Performance Synthesis
        self.regime_history = deque(maxlen=100) # For persistence calculation
        self.tick_buffer = deque(maxlen=300)

        # External API connections are centralized in DataGateway
        self.api = None

    def _pin_core(self):
        if sys.platform != "win32":
            try:
                os.sched_setaffinity(0, {self.core_pin})
                logger.info(f"[{self.asset_id}] Pinned to Core {self.core_pin} natively.")
            except Exception as e:
                logger.error(f"[{self.asset_id}] Failed to pin core: {e}")

    async def _fetch_parameters(self):
        """Loads RV/ADX/IV/PCR/VPIN from SHM with Redis fallback."""
        try:
            now_ts = time.time()
            # 1. Primary: Constant ingestion from SignalVector SHM
            sig = self.shm_signals.read()
            if sig:
                # 1. Pull from SignalVector object attributes (Not .get())
                self.rv_val = float(sig.rv)
                self.adx_val = float(sig.adx)
                self.vpin_val = float(sig.vpin)
                self.last_pcr = float(sig.pcr)
                self.pcr_roc = float(sig.pcr_roc)
                self.ofi_z = float(sig.ofi_z)
                self.iv_rv_spread = float(sig.iv_rv_spread)
                self.iv_val = float(sig.iv_atm)
                
                # Synchronize raw price peaks (Highs/Lows) for trend calculation
                self.day_high = float(sig.day_high) 
                self.day_low = float(sig.day_low)   
                
                self.stale_override = bool(sig.stale_flag)
                return # Successful SHM read - no need for Redis poll
            
            # 2. Fallback: Standardize parameters from Redis if SHM stall detected
            self.stale_override = True 
            history_raw = await self.r.get(f"history_14d:{self.asset_id}")
            if history_raw:
                self.history_14d = json.loads(history_raw)
                rv_closes = self.history_14d[-11:] if len(self.history_14d) >= 11 else self.history_14d
                self.rv_val = self._calculate_realized_vol(rv_closes)
                
                # Fetch Intraday wicks for high-fidelity ADX
                h_raw = await self.r.get(f"DAY_HIGH:{self.asset_id}")
                l_raw = await self.r.get(f"DAY_LOW:{self.asset_id}")
                self.day_high = float(h_raw) if h_raw else self.history_14d[-1]
                self.day_low = float(l_raw) if l_raw else self.history_14d[-1]
                
                self.adx_val = self._calculate_adx_approximation(self.history_14d, self.day_high, self.day_low)
            
            pcr_raw = await self.r.get(f"live_pcr:{self.asset_id}")
            if pcr_raw: self.last_pcr = float(pcr_raw)

            vpin_raw = await self.r.get(f"vpin:{self.asset_id}") or await self.r.get("vpin")
            if vpin_raw: self.vpin_val = float(vpin_raw)

        except Exception as e:
            logger.error(f"[{self.asset_id}] Parameter fetch error: {e}")

    # _load_lookback_shoonya REMOVED (Centralized in DataGateway)

    def _calculate_realized_vol(self, closes: list[float]) -> float:
        """Calculates 14-day Annualized Realized Volatility."""
        if len(closes) < 2: return 0.20 # Default 20%
        log_returns = np.diff(np.log(closes))
        # Daily Std Dev * Sqrt(252 trading days)
        rv = np.std(log_returns) * np.sqrt(252)
        return float(rv)

    def _calculate_adx_approximation(self, closes: list[float], intraday_high: float, intraday_low: float) -> float:
        """Calculates Trend Strength (ADX) using intraday price extremes."""
        if len(closes) < 14: return 20.0
        
        # Build O/H/L/C proxy for the trend window
        # We use closes for history, but use the provided wicks for the current 'candle'
        c_arr = np.array(closes)
        h_arr = np.array(closes) # Default history high to close
        l_arr = np.array(closes) # Default history low to close
        
        # Inject intraday wicks into the final element
        h_arr[-1] = max(h_arr[-1], intraday_high)
        l_arr[-1] = min(l_arr[-1], intraday_low)
        
        up_move = h_arr[1:] - h_arr[:-1]
        down_move = l_arr[:-1] - l_arr[1:]
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        # True Range approximation
        tr = np.maximum(h_arr[1:] - l_arr[1:], 
                        np.maximum(np.abs(h_arr[1:] - c_arr[:-1]), 
                                   np.abs(l_arr[1:] - c_arr[:-1])))
        
        tr_sum = np.sum(tr[-14:])
        if tr_sum < 1e-9: return 0.0
        
        plus_di = 100 * (np.sum(plus_dm[-14:]) / tr_sum)
        minus_di = 100 * (np.sum(minus_dm[-14:]) / tr_sum)
        
        div = plus_di + minus_di
        dx = 100 * (np.abs(plus_di - minus_di) / div) if div > 0 else 0
        return float(dx)

    def classify_regime(self) -> int:
        """
        Classifies the market state using Hysteresis to prevent frequent flipping.
        Uses adaptive entry/exit thresholds for stability.
        """
        curr = self.current_regime_int
        
        # Safety Check: Halt if data is stale or market toxicity (VPIN) is too high
        if self.stale_override or self.vpin_val > 0.8 or self.rv_val > 0.50:
            return 3 # VOLATILE/SAFETY_HALT

        # --- TRENDING (Regime 1) ---
        # Enter if ADX > 25, Stay until ADX < 20
        if curr == 1:
            if self.adx_val > 20: return 1
        else:
            if self.adx_val > 25: return 1

        # --- RANGING (Regime 2) ---
        # Enter if IV-RV edge exists and ADX is low
        # Stay until edge disappears or Trend takes over
        if curr == 2:
            if self.iv_rv_spread > 0.01 and self.adx_val < 22: return 2
        else:
            if self.iv_rv_spread > 0.03 and self.adx_val < 20: return 2

        # --- DEFAULT (Regime 0) ---
        return 0 # NEUTRAL

    async def run(self):
        self._pin_core()
        logger.info(f"Regime Detector [{self.asset_id}] active. Deterministic regime mode.")
        
        # Initial param fetch
        await self._fetch_parameters()
        
        # Subscribe to market state for intraday updates
        topic = f"{Topics.MARKET_STATE}.{self.asset_id}"
        sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=[topic, "STATE"])
        
        param_sync_tick = 0
        while True:
            try:
                msg_topic, state = await self.mq.recv_json(sub)
                if not state or state.get("symbol") != self.asset_id:
                    continue
                
                # Sync parameters (PCR/History) every 300 market state ticks (~5 mins)
                param_sync_tick += 1
                if param_sync_tick >= 300:
                    await self._fetch_parameters()
                    param_sync_tick = 0

                # Determine deterministic regime integer with hysteresis
                s18_val = self.classify_regime()
                self.current_regime_int = s18_val
                self.regime_history.append(s18_val)
                
                # S26: Persistence (% of recent history in this state)
                s26_val = (list(self.regime_history).count(s18_val) / len(self.regime_history)) * 100.0
                
                # S27: Multi-Factor Quality (Confidence Depth)
                if s18_val == 1: # TRENDING
                    s27_val = min(100.0, (self.adx_val / 50.0) * 100.0)
                elif s18_val == 2: # RANGING
                    # High quality range = Low RV relative to historical norm (proxy 0.15)
                    s27_val = min(100.0, (0.15 / max(0.01, self.rv_val)) * 100.0)
                elif s18_val == 3: # VOLATILE
                    # Toxicity certainty based on VPIN height
                    s27_val = min(100.0, self.vpin_val * 100.0)
                else: # NEUTRAL
                    s27_val = 50.0
                
                # Map to legacy strings for dashboard/paper compat
                regime_map = {0: "NEUTRAL", 1: "TRENDING", 2: "RANGING", 3: "VOLATILE"}
                legacy_regime = regime_map.get(s18_val, "NEUTRAL")
                
                # Write Command Packet to Shared Memory
                self.shm_regime.write(RegimeVector(
                    s18_int=s18_val,
                    s26_persistence=round(s26_val, 2),
                    s27_quality=round(s27_val, 2),
                    veto=(s18_val == 3),
                    timestamp=time.time()
                ))

                # Push full telemetry to Redis for dashboard
                await self.r.set(f"regime_state:{self.asset_id}", json.dumps({
                    "regime": legacy_regime,
                    "s18_int": s18_val,
                    "s26_persistence": round(s26_val, 2),
                    "s27_quality": round(s27_val, 2),
                    "rv": self.rv_val,
                    "adx": self.adx_val,
                    "pcr": self.last_pcr,
                    "pcr_roc": self.pcr_roc,
                    "iv_rv_spread": self.iv_rv_spread,
                    "vpin": self.vpin_val,
                    "veto_active": (s18_val == 3),
                    "stale_data": self.stale_override,
                    "timestamp": datetime.now().isoformat()
                }, cls=NumpyEncoder))
                
                # Set per-asset regime key 
                await self.r.set(f"current_regime:{self.asset_id}", legacy_regime)
                if self.asset_id == "NIFTY50":
                    await self.r.set("current_regime", legacy_regime)
                
            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Regime Detector [{self.asset_id}] loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Configure monitoring for a specific index (NIFTY50, BANKNIFTY, SENSEX)
    parser.add_argument("--asset", required=True)
    parser.add_argument("--core", type=int, required=True)
    args = parser.parse_args()

    # Robust normalization
    asset = args.asset.strip().upper()
    valid_assets = ["NIFTY50", "BANKNIFTY", "SENSEX"]
    if asset not in valid_assets:
        print(f"Error: Invalid asset {asset}. Choose from {valid_assets}")
        sys.exit(1)

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    engine = RegimeDetector(asset, args.core)
    asyncio.run(engine.run())
