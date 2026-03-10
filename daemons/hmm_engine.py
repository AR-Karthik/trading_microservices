import argparse
import asyncio
import json
import logging
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections import deque
import numpy as np
import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("HmmEngine")

try:
    from hmmlearn.hmm import GMMHMM
    HAS_HMM = True
except ImportError:
    HAS_HMM = False

REGIME_STATES = {0: "RANGING", 1: "TRENDING", 2: "CRASH"}

class HMMEngine:
    def __init__(self, asset_id: str, core_pin: int):
        self.asset_id = asset_id
        self.core_pin = core_pin
        self.mq = MQManager()
        self.model_path = f"data/models/hmm_v_{self.asset_id.lower()}.pkl"
        self.hmm_model = self._load_model()
        self.feature_buffer = deque(maxlen=300)
        
        # Redis connection
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self.r = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)

    def _pin_core(self):
        if sys.platform != "win32":
            try:
                os.sched_setaffinity(0, {self.core_pin})
                logger.info(f"[{self.asset_id}] Pinned to Core {self.core_pin} natively.")
            except Exception as e:
                logger.error(f"[{self.asset_id}] Failed to pin core: {e}")

    def _load_model(self):
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    return pickle.load(f)
            except Exception as e:
                logger.error(f"Failed to load {self.model_path}: {e}")
        return None

    def heuristic_regime(self, features):
        arr = np.array(features)
        ofi_z = arr[:, 0]
        rv = arr[:, 1]
        if np.abs(ofi_z.mean()) > 1.5 and rv.mean() > 0.001: return "TRENDING"
        if rv.mean() > 0.003: return "CRASH"
        return "RANGING"

    async def run(self):
        self._pin_core()
        logger.info(f"HMM Engine [{self.asset_id}] active. Listening for market ticks.")
        
        # Subscribe specifically to this asset's market state
        topic = f"{Topics.MARKET_STATE}.{self.asset_id}"
        sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=[topic, "STATE"])
        
        tick_count = 0
        while True:
            try:
                msg_topic, state = await self.mq.recv_json(sub)
                if not state or state.get("asset") != self.asset_id:
                    continue
                
                tick_count += 1
                if tick_count % 10 != 0:
                    continue

                feat = [
                    state.get("log_ofi_zscore", 0.0),
                    state.get("rv", 0.0),
                    state.get("basis_zscore", 0.0),
                    state.get("vol_term_ratio", 1.0)
                ]
                self.feature_buffer.append(feat)

                if len(self.feature_buffer) < 60:
                    regime, prob = "RANGING", 0.6
                else:
                    X = np.array(self.feature_buffer)
                    
                    if not HAS_HMM or self.hmm_model is None:
                        regime = self.heuristic_regime(list(self.feature_buffer)[-30:])
                        prob = 0.6
                    else:
                        is_warmup = await self.r.get("HMM_WARM_UP") == "True"
                        try:
                            log_prob, state_seq = self.hmm_model.decode(X, lengths=[len(X)])
                            latest_state = int(state_seq[-1])
                            regime = REGIME_STATES.get(latest_state, "RANGING")
                            
                            if is_warmup:
                                regime = "WAITING"
                                
                            probas = self.hmm_model.predict_proba(X)
                            prob = float(probas[-1, latest_state])
                        except Exception:
                            regime = self.heuristic_regime(list(self.feature_buffer)[-10:])
                            prob = 0.6
                
                # Push back to Redis for Meta-Router evaluation
                await self.r.hset(f"hmm_regime_state", self.asset_id, json.dumps({
                    "regime": regime,
                    "prob": prob,
                    "timestamp": time.time()
                }))
                
            except Exception as e:
                logger.error(f"HMM Engine [{self.asset_id}] loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True, choices=["NIFTY", "BANKNIFTY", "SENSEX"])
    parser.add_argument("--core", type=int, required=True)
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    engine = HMMEngine(args.asset, args.core)
    asyncio.run(engine.run())
