"""
daemons/meta_router.py
======================
Tri-Brain HMM Dispatcher & Portfolio Allocation (V6.4)

Architecture:
  Main asyncio loop   → Reads Tri-Index Regimes from Redis/SHM; enforces Global Vetoes;
                        Dispatches commands to the Execution Bridge via ZMQ.
  Strategy Logic      → Asset-specific classes (Nifty, BankNifty, Sensex) for allocation weighting.

"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics
from core.shm import ShmManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MetaRouter")

class BaseStrategyLogic:
    def __init__(self, asset_id: str, redis_client):
        self.asset_id = asset_id
        self.r = redis_client

    async def calculate_weights(self, state: dict, regime: dict) -> list[dict]:
        """Calculates fractional kelly allocations based on the regime."""
        commands = []
        try:
            p = float(regime.get("prob", 0.6))
            r_type = regime.get("regime", "RANGING")
            
            b = 1.5
            kelly_f = p - ((1.0 - p) / b)
            fractional_kelly = max(0.01, 0.5 * kelly_f)
            strat_weight = min(0.40, fractional_kelly) if r_type == "TRENDING" else min(0.20, fractional_kelly)
            
            # Allocation execution is handled by the Bridge, we just send weight/lots intent
            commands.append({"asset": self.asset_id, "regime": r_type, "weight": strat_weight})
        except Exception as e:
            logger.error(f"Logic failure on {self.asset_id}: {e}")
            commands.append({"asset": self.asset_id, "regime": "RANGING", "weight": 0.01})
        return commands

class NiftyStrategyLogic(BaseStrategyLogic):
    def __init__(self, r): super().__init__("NIFTY", r)

class BankNiftyStrategyLogic(BaseStrategyLogic):
    def __init__(self, r): super().__init__("BANKNIFTY", r)

class SensexStrategyLogic(BaseStrategyLogic):
    def __init__(self, r): super().__init__("SENSEX", r)

class MetaRouter:
    def __init__(self, test_mode: bool = False):
        self.test_mode = test_mode
        self.mq = MQManager()
        
        if not test_mode:
            self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)
            redis_host = os.getenv("REDIS_HOST", "localhost")
            self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
            self.shm = ShmManager(mode='r')
        else:
            self.shm = None

        # The Tri-Brain Subsystems
        self.brains = {
            "NIFTY": NiftyStrategyLogic(self._redis if not test_mode else None),
            "BANKNIFTY": BankNiftyStrategyLogic(self._redis if not test_mode else None),
            "SENSEX": SensexStrategyLogic(self._redis if not test_mode else None),
        }
    
    async def _check_cross_index_divergence(self, regimes: dict) -> bool:
        """Returns True if the markets are severely fractured (divergence veto)."""
        trends = [r.get("regime") for r in regimes.values()]
        if "TRENDING" in trends and "CRASH" in trends:
            logger.warning("FRACTURED MARKET VETO: Divergent extreme regimes detected.")
            return True
        return False

    async def broadcast_decisions(self, market_state: dict, regimes: dict):
        """Formulate execution payloads."""
        all_commands = []
        is_fractured = await self._check_cross_index_divergence(regimes)

        for asset, logic in self.brains.items():
            regime = regimes.get(asset, {"regime": "WAITING", "prob": 0.0})
            
            if is_fractured:
                regime["regime"] = "WAITING" # Veto active
                
            cmds = await logic.calculate_weights(market_state.get(asset, {}), regime)
            all_commands.extend(cmds)

        if not self.test_mode:
            for cmd in all_commands:
                target = cmd.get("asset", "ALL")
                await self.mq.send_json(self.cmd_pub, cmd, topic=target)

    async def run(self):
        logger.info("MetaRouter [Core 3] Tri-Brain active. Waiting for market & model convergence...")
        
        while True:
            try:
                # Polling the Tri-Brain HMM states from Redis (In reality, Lock-Free /dev/shm)
                regimes_raw = await self._redis.hgetall("hmm_regime_state") if not self.test_mode else {}
                regimes = {k: json.loads(v) for k, v in regimes_raw.items()}

                if len(regimes) == 3:
                     # In a real tick-by-tick system, we pull the live Spot state for all 3
                    mock_market_state = {"NIFTY": {}, "BANKNIFTY": {}, "SENSEX": {}}
                    await self.broadcast_decisions(mock_market_state, regimes)
                
                await asyncio.sleep(0.1) # 100ms router interval 

            except Exception as e:
                logger.error(f"Router Exception: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Core Pinning happens via taskset wrapper in shell or python PSUtil
    # Since this is the orchestrator, it runs on Core 3
    router = MetaRouter()
    asyncio.run(router.run())
