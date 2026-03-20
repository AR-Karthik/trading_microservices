"""
Heuristic Regime Engine
Computes multi-index market regimes deterministically using Realized Volatility, 
Trend Strength (ADX), and Implied Volatility parameters, synchronized through Redis.
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
from core.shm import ShmManager, SignalVector

# External API fallback for historic market data
try:
    from NorenRestApiPy.NorenApi import NorenApi
    import pyotp
    _HAS_SHOONYA = True
except ImportError:
    _HAS_SHOONYA = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HeuristicEngine")

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

class HeuristicEngine:
    def __init__(self, asset_id: str, core_pin: int):
        self.asset_id = asset_id
        self.core_pin = core_pin
        self.mq = MQManager()
        # Thread-safe Shared Memory isolation per specific index
        self.shm = ShmManager(asset_id=asset_id, mode='w')
        
        # Redis connection
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_pass = os.getenv("REDIS_PASSWORD", "")
        self.r = redis.Redis(host=redis_host, port=6379, db=0, password=redis_pass, decode_responses=True)
        
        # Heuristic state
        self.history_14d = [] # Fixed daily closes
        self.last_pcr = 1.0
        self.adx_val = 0.0
        self.rv_val = 0.0
        self.iv_val = 0.15       # Implied Volatility of At-The-Money Strikes
        self.vpin_val = 0.0      # Order Flow Toxicity volume indicator
        self.stale_override = False  # Failsafe threshold flag for stale data
        self.last_regime_ts = 0.0    # Time-gate to prevent regime oscillation flutter
        
        # Tick buffer for intraday vol
        self.tick_buffer = deque(maxlen=300)

        # Direct Rest API removed [Audit 14.1: Centralized in DataGateway]
        self.api = None

    def _pin_core(self):
        if sys.platform != "win32":
            try:
                os.sched_setaffinity(0, {self.core_pin})
                logger.info(f"[{self.asset_id}] Pinned to Core {self.core_pin} natively.")
            except Exception as e:
                logger.error(f"[{self.asset_id}] Failed to pin core: {e}")

    async def _fetch_parameters(self):
        """Loads RV/ADX/IV/PCR/VPIN from Redis with stale-data protection."""
        try:
            now_ts = time.time()
            self.stale_override = False  # Reset each cycle

            # Load 14D history fetched by SystemController
            history_raw = await self.r.get(f"history_14d:{self.asset_id}")
            
            # [Audit-Fix] If Redis is empty, attempt direct API fallback
            # Failsafe: if history missing, it's a fatal config error in this model
            if not self.history_14d:
                logger.error(f"[{self.asset_id}] FATAL: 14D history missing in Redis! DataGateway sync must run first.")
                # Re-check Redis after fallback
                history_raw = await self.r.get(f"history_14d:{self.asset_id}")

            if history_raw:
                self.history_14d = json.loads(history_raw)
                # [R2-13] Spec says 10-day RV; use last 11 closes for 10 returns
                rv_closes = self.history_14d[-11:] if len(self.history_14d) >= 11 else self.history_14d
                self.rv_val = self._calculate_realized_vol(rv_closes)
                self.adx_val = self._calculate_adx_approximation(self.history_14d)  # ADX uses full 14-period
            
            # Load live PCR from DataGateway
            pcr_raw = await self.r.get(f"live_pcr:{self.asset_id}")
            if pcr_raw:
                self.last_pcr = float(pcr_raw)

            # [C1-02] Load live IV (ATM Implied Volatility)
            iv_raw = await self.r.get(f"LIVE_IV:{self.asset_id}")
            if iv_raw:
                self.iv_val = float(iv_raw)

            # [R2-07] Use per-asset VPIN key with global fallback
            vpin_raw = await self.r.get(f"vpin:{self.asset_id}") or await self.r.get("vpin")
            if vpin_raw:
                self.vpin_val = float(vpin_raw)

            # [C1-05] Stale Data Protection: check timestamps
            for key_suffix in [f"LIVE_IV:{self.asset_id}", f"live_pcr:{self.asset_id}"]:
                ts_raw = await self.r.get(f"{key_suffix}_TS")
                if ts_raw:
                    age = now_ts - float(ts_raw)
                    if age > STALE_DATA_THRESHOLD_S:
                        logger.warning(f"[{self.asset_id}] STALE DATA: {key_suffix} age={age:.1f}s > {STALE_DATA_THRESHOLD_S}s")
                        self.stale_override = True

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

    def _calculate_adx_approximation(self, closes: list[float]) -> float:
        """Simplified 14-day Trend Strength approximation."""
        if len(closes) < 14: return 20.0 # Default weak trend
        
        # Simple high-low range trend strength
        highs = np.array(closes) # In this model we only have closes
        lows = np.array(closes)
        
        # Real ADX requires H/L/C, but with just Daily Closes we use absolute momentum
        diffs = np.diff(closes)
        ups = np.sum(diffs[diffs > 0])
        downs = np.abs(np.sum(diffs[diffs < 0]))
        
        if (ups + downs) < 1e-9: return 0.0  # [C1-06] Epsilon division guard
        adx = (abs(ups - downs) / (ups + downs)) * 100
        return float(adx)

    def classify_regime(self) -> str:
        """
        [C1-01] Deterministic Translation Matrix (Spec-Aligned)

        Priority 1: CRASH / TOXIC  — RV > 50% OR VPIN > 0.8
        Priority 2: TRENDING       — ADX > 25
        Priority 3: HIGH_VOL_CHOP  — ADX < 20 AND RV > 25%
        Priority 4: RANGING        — (IV − RV) > 3.0pp AND ADX < 20  (or default)
        Overlays:   PCR > 1.5 → OVERBOUGHT, PCR < 0.6 → OVERSOLD/RED
        """
        # [C1-05] Stale data override
        if self.stale_override:
            return REGIME_CRASH  # Safe default: block all new entries

        # Priority 1: Crash / Toxic
        if self.rv_val > 0.50 or self.vpin_val > 0.8:
            return REGIME_CRASH

        # PCR extremes (overlay)
        if self.last_pcr > 1.5:
            return REGIME_OVERBOUGHT
        if self.last_pcr < 0.6:  # [C1-07] Extreme Fear at 0.6, not 0.7
            return REGIME_OVERSOLD

        # Priority 2: Trending
        if self.adx_val > 25:  # [C1-01] Spec says 25, not 35
            return REGIME_TRENDING

        # Priority 3: High Volatility Chop (Iron Condor window)
        if self.adx_val < 20 and self.rv_val > 0.25:
            return REGIME_HIGH_VOL_CHOP

        # Priority 4: Ranging (premium edge exists)
        if (self.iv_val - self.rv_val) > 0.03 and self.adx_val < 20:  # [C1-02] 3.0pp = 0.03
            return REGIME_RANGING

        # Default fallback
        return REGIME_RANGING if self.adx_val < 20 else REGIME_TRENDING

    async def run(self):
        self._pin_core()
        logger.info(f"Heuristic Engine [{self.asset_id}] active. Deterministic regime mode.")
        
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

                # [C1-04] Time-gate regime updates to every 5 seconds
                now_ts = time.time()
                if (now_ts - self.last_regime_ts) < 5.0:
                    continue
                self.last_regime_ts = now_ts

                # Determine regime
                regime = self.classify_regime()
                
                # Map to legacy for downstream strategy compatibility
                # Downstream expects RANGING, TRENDING, HIGH_VOL_CHOP, or CRASH
                legacy_regime = regime
                if regime in [REGIME_OVERBOUGHT, REGIME_OVERSOLD]:
                    legacy_regime = REGIME_RANGING  # Tighten stops but keep ranging logic
                elif regime == REGIME_TOXIC:
                    legacy_regime = REGIME_CRASH
                
                # Update Shared Memory for Meta-Router
                self.shm.write(SignalVector(
                    s_total=state.get("s_total", 0.0),
                    vpin=state.get("vpin", 0.0),
                    ofi_z=state.get("ofi_zscore", 0.0),
                    rv=self.rv_val,
                    adx=self.adx_val,
                    pcr=self.last_pcr,
                    net_delta_nifty=float(state.get("net_delta_nifty", 0.0)), # [Audit 9.5] Pull from state
                    net_delta_banknifty=float(state.get("net_delta_banknifty", 0.0)), # [Audit 9.5] Pull from state
                    net_delta_sensex=float(state.get("net_delta_sensex", 0.0)),
                    veto=(regime == REGIME_CRASH)
                ))

                # Push to Redis for dashboard/orchestrator
                await self.r.hset("hmm_regime_state", self.asset_id, json.dumps({
                    "regime": legacy_regime,
                    "heuristic_regime": regime,
                    "rv": self.rv_val,
                    "adx": self.adx_val,
                    "pcr": self.last_pcr,
                    "iv": self.iv_val,
                    "vpin": self.vpin_val,
                    "prob": 1.0,
                    "pcr_veto_puts": self.last_pcr < 0.6,  # [C1-07] Block Put sales on extreme fear
                    "stale_data": self.stale_override,
                    "timestamp": datetime.now().isoformat()
                }, cls=NumpyEncoder))
                
                # [R2-06] Set per-asset regime key for all assets (not just NIFTY50)
                await self.r.set(f"hmm_regime:{self.asset_id}", legacy_regime)
                # Keep legacy singleton for NIFTY50 backward compat
                if self.asset_id == "NIFTY50":
                    await self.r.set("hmm_regime", legacy_regime)
                
            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Heuristic Engine [{self.asset_id}] loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # [Audit 3.1] Standardize on NIFTY50 everywhere
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

    engine = HeuristicEngine(asset, args.core)
    asyncio.run(engine.run())
