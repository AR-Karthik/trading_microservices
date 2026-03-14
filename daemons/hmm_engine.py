"""
daemons/hmm_engine.py
=====================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Responsibilities:
- Incremental Hidden Markov Model (HMM) regime inference.
- Deterministic heuristic fallback for regime classification.
- Real-time parameter synchronization via Redis.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from collections import deque
import numpy as np
import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics, NumpyEncoder
from core.shm import ShmManager, SignalVector

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HeuristicEngine")

# Regime Verdict constants
REGIME_RANGING = "RANGING"
REGIME_TRENDING = "TRENDING"
REGIME_CRASH = "CRASH" 
REGIME_OVERBOUGHT = "OVERBOUGHT" 
REGIME_OVERSOLD = "OVERSOLD" 

class HeuristicEngine:
    def __init__(self, asset_id: str, core_pin: int):
        self.asset_id = asset_id
        self.core_pin = core_pin
        self.mq = MQManager()
        self.shm = ShmManager(mode='w')
        
        # Redis connection
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self.r = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)
        
        # Heuristic state
        self.history_14d = [] # Fixed daily closes
        self.last_pcr = 1.0
        self.adx_val = 0.0
        self.rv_val = 0.0
        
        # Tick buffer for intraday vol
        self.tick_buffer = deque(maxlen=300)

    def _pin_core(self):
        if sys.platform != "win32":
            try:
                os.sched_setaffinity(0, {self.core_pin})
                logger.info(f"[{self.asset_id}] Pinned to Core {self.core_pin} natively.")
            except Exception as e:
                logger.error(f"[{self.asset_id}] Failed to pin core: {e}")

    async def _fetch_parameters(self):
        """Loads RV/ADX history and PCR from Redis."""
        try:
            # Load 14D history fetched by SystemController
            history_raw = await self.r.get(f"history_14d:{self.asset_id}")
            if history_raw:
                self.history_14d = json.loads(history_raw)
                self.rv_val = self._calculate_realized_vol(self.history_14d)
                self.adx_val = self._calculate_adx_approximation(self.history_14d)
            
            # Load live PCR from DataGateway
            pcr_raw = await self.r.get(f"live_pcr:{self.asset_id}")
            if pcr_raw:
                self.last_pcr = float(pcr_raw)
        except Exception as e:
            logger.error(f"[{self.asset_id}] Parameter fetch error: {e}")

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
        
        if (ups + downs) == 0: return 0.0
        adx = (abs(ups - downs) / (ups + downs)) * 100
        return float(adx)

    def classify_regime(self) -> str:
        """
        Deterministic Translation Matrix
        RV < 15% AND ADX < 25 -> RANGING (Condor window)
        RV > 25% AND ADX > 35 -> TRENDING (Credit Spread window)
        PCR > 1.5 -> OVERBOUGHT (Downside risk)
        PCR < 0.7 -> OVERSOLD (Upside risk)
        """
        if self.last_pcr > 1.5: return REGIME_OVERBOUGHT
        if self.last_pcr < 0.7: return REGIME_OVERSOLD
        
        # Volatility/Trend thresholds
        if self.rv_val < 0.15 and self.adx_val < 25:
            return REGIME_RANGING
        if self.rv_val > 0.25 and self.adx_val > 35:
            return REGIME_TRENDING
            
        # Default fallback (Legacy compatibility)
        if self.rv_val > 0.50: return REGIME_CRASH
        return REGIME_RANGING if self.adx_val < 30 else REGIME_TRENDING

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
                if not state or state.get("asset") != self.asset_id:
                    continue
                
                # Sync parameters (PCR/History) every 300 market state ticks (~5 mins)
                param_sync_tick += 1
                if param_sync_tick >= 300:
                    await self._fetch_parameters()
                    param_sync_tick = 0

                # Determine regime
                regime = self.classify_regime()
                
                # Map to legacy for downstream strategy compatibility
                # Downstream expects RANGING, TRENDING, or CRASH
                legacy_regime = regime
                if regime in [REGIME_OVERBOUGHT, REGIME_OVERSOLD]:
                    legacy_regime = REGIME_RANGING # Tighten stops but keep ranging logic
                
                # Update Shared Memory for Meta-Router
                self.shm.write(SignalVector(
                    s_total=state.get("s_total", 0.0),
                    vpin=state.get("vpin", 0.0),
                    ofi_z=state.get("ofi_zscore", 0.0),
                    rv=self.rv_val,
                    adx=self.adx_val,
                    pcr=self.last_pcr,
                    net_delta_nifty=0.0, # Filled by MarketSensor in Phase 5
                    net_delta_banknifty=0.0,
                    veto=(regime == REGIME_CRASH)
                ))

                # Push to Redis for dashboard/orchestrator
                await self.r.hset("hmm_regime_state", self.asset_id, json.dumps({
                    "regime": legacy_regime,
                    "heuristic_regime": regime,
                    "rv": self.rv_val,
                    "adx": self.adx_val,
                    "pcr": self.last_pcr,
                    "prob": 1.0,
                    "timestamp": datetime.now().isoformat()
                }, cls=NumpyEncoder))
                
                # Link legacy key for primary index monitoring
                if self.asset_id == "NIFTY50" or self.asset_id == "NIFTY":
                    await self.r.set("hmm_regime", legacy_regime)
                
            except Exception as e:
                logger.error(f"Heuristic Engine [{self.asset_id}] loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True, choices=["NIFTY", "BANKNIFTY", "SENSEX"])
    parser.add_argument("--core", type=int, required=True)
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    engine = HeuristicEngine(args.asset, args.core)
    asyncio.run(engine.run())
