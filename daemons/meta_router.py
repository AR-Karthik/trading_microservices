"""
daemons/meta_router.py
======================
Tri-Brain HMM Dispatcher & Portfolio Allocation (V6.4)

Architecture:
  Main asyncio loop   → Reads Market State and Firestore settings;
                        Delegates to RegimeOrchestrator;
                        Dispatches commands via ZMQ.
  RegimeOrchestrator  → Wrapper managing Providers (HMM, Deterministic, Hybrid).
  Providers           → Logic for regime classification and signal weighting.

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

    async def calculate_weights(self, state: dict, regime_context: dict) -> dict:
        """Calculates fractional kelly allocations based on the provided context."""
        try:
            p = float(regime_context.get("prob", 0.6))
            r_type = regime_context.get("regime", "RANGING")
            provider = regime_context.get("provider", "UNKNOWN")
            
            b = 1.5
            kelly_f = p - ((1.0 - p) / b)
            fractional_kelly = max(0.01, 0.5 * kelly_f)
            
            # Allocation execution is handled by the Bridge, we just send weight/lots intent
            return {
                "asset": self.asset_id, 
                "regime": r_type, 
                "weight": fractional_kelly, 
                "provider": provider,
                "score": p
            }
        except Exception as e:
            logger.error(f"Logic failure on {self.asset_id}: {e}")
            return {"asset": self.asset_id, "regime": "RANGING", "weight": 0.01, "provider": "ERROR"}

# ── Providers: The Decision Engines ───────────────────────────────────────────

class HMMProvider:
    async def get_context(self, asset: str, state: dict, redis_client) -> dict:
        """Provider A: HMM (Probabilistic)"""
        raw = await redis_client.hget("hmm_regime_state", asset)
        regime = json.loads(raw) if raw else {"regime": "RANGING", "prob": 0.5}
        regime["provider"] = "HMM"
        return regime

class DeterministicProvider:
    async def get_context(self, asset: str, state: dict, redis_client) -> dict:
        """Provider B: Deterministic (Mathematical)"""
        hurst = state.get("hurst", 0.5)
        er = state.get("kaufman_er", 0.5)
        adx = state.get("adx", 20.0)
        
        regime = "RANGING"
        prob = 0.5
        
        if hurst > 0.55 and adx > 25 and er > 0.6:
            regime = "TRENDING"
            prob = min(0.9, 0.5 + (hurst - 0.5) + (adx - 20)/100)
        elif hurst < 0.45:
            regime = "RANGING"
            prob = 0.7
            
        return {"regime": regime, "prob": prob, "provider": "DETERMINISTIC"}

class HybridProvider:
    def __init__(self, hmm, det):
        self.hmm = hmm
        self.det = det

    async def get_context(self, asset: str, state: dict, redis_client) -> dict:
        """Provider C: Hybrid (Weighted Consensus)"""
        h_ctx = await self.hmm.get_context(asset, state, redis_client)
        d_ctx = await self.det.get_context(asset, state, redis_client)
        
        # Weighted Score: 40% HMM, 60% Math (Hurst/ER/ADX)
        score = (0.4 * h_ctx["prob"]) + (0.6 * d_ctx["prob"])
        
        regime = "WAITING"
        if score > 0.7:
            # Dual-key agreement required for Trending
            if h_ctx["regime"] == "TRENDING" and d_ctx["regime"] == "TRENDING":
                regime = "TRENDING"
            else:
                regime = "RANGING" # Consensus says trend isn't confirmed
                
        return {"regime": regime, "prob": score, "provider": "HYBRID"}

# ── The Regime Orchestrator ───────────────────────────────────────────────────

class RegimeOrchestrator:
    def __init__(self, hmm, det, hybrid):
        self.providers = {
            "HMM": hmm,
            "DETERMINISTIC": det,
            "HYBRID": hybrid
        }
        self.active_engine = "HYBRID" # Default conservative

    async def get_decisions(self, asset: str, state: dict, redis_client, logic_class):
        """Returns active decision + shadow signals for attribution."""
        results = {}
        for name, provider in self.providers.items():
            ctx = await provider.get_context(asset, state, redis_client)
            decision = await logic_class.calculate_weights(state, ctx)
            results[name] = decision
        
        # Return the active one specifically marked
        active_dec = results.get(self.active_engine, results["HYBRID"])
        return active_dec, results

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

        # Setup Orchestrator
        hmm = HMMProvider()
        det = DeterministicProvider()
        self.orchestrator = RegimeOrchestrator(hmm, det, HybridProvider(hmm, det))
        
        # Asset Logic
        self.brains = {
            "NIFTY": BaseStrategyLogic("NIFTY", self._redis if not test_mode else None),
            "BANKNIFTY": BaseStrategyLogic("BANKNIFTY", self._redis if not test_mode else None),
            "SENSEX": BaseStrategyLogic("SENSEX", self._redis if not test_mode else None),
        }
    
    async def _sync_settings(self):
        """Syncs engine mode and parameters from Redis (upstream Firestore)."""
        mode = await self._redis.get("active_regime_engine")
        if mode in self.orchestrator.providers:
            self.orchestrator.active_engine = mode

    async def _check_cross_index_divergence(self, regimes: dict) -> bool:
        """Returns True if the markets are severely fractured (divergence veto)."""
        trends = [r.get("regime") for r in regimes.values()]
        if "TRENDING" in trends and "CRASH" in trends:
            logger.warning("FRACTURED MARKET VETO: Divergent extreme regimes detected.")
            return True
        return False

    async def broadcast_decisions(self, market_state: dict, regimes: dict = None):
        """Formulate execution payloads and attribution logs."""
        if regimes is None: regimes = {}
        all_commands = []
        attribution_payloads = []

        await self._sync_settings()

        is_fractured = await self._check_cross_index_divergence(regimes)

        for asset, logic in self.brains.items():
            state = market_state.get(asset, {})
            regime = regimes.get(asset, {"regime": "WAITING"})
            
            if is_fractured:
                regime["regime"] = "WAITING" # Veto active
                
            active_dec, all_decs = await self.orchestrator.get_decisions(
                asset, state, self._redis, logic
            )
            
            # Recalculate weights based on the regime
            cmds = await logic.calculate_weights(state, regime)
            all_commands.extend(cmds)
            all_commands.append(active_dec)
            attribution_payloads.append({
                "asset": asset,
                "timestamp": datetime.now().isoformat(),
                "decisions": all_decs,
                "active_engine": self.orchestrator.active_engine
            })

        if not self.test_mode:
            for cmd in all_commands:
                await self.mq.send_json(self.cmd_pub, cmd, topic=cmd["asset"])
            
            # Log attributions for Sidecar to push to Firestore
            await self._redis.set("latest_attributions", json.dumps(attribution_payloads))

    async def run(self):
        logger.info("MetaRouter [Core 3] Tri-Brain active. Waiting for market & model convergence...")
        
        while True:
            try:
                # Fetch latest market state for all assets
                state_raw = await self._redis.get("latest_market_state")
                state = json.loads(state_raw) if state_raw else {}
                
                # Polling the Tri-Brain HMM states from Redis
                regimes_raw = await self._redis.hgetall("hmm_regime_state") if not self.test_mode else {}
                regimes = {k: json.loads(v) for k, v in regimes_raw.items()}

                # In v6.5, MetaRouter is state-aware, not just regime-aware
                if len(regimes) == 3 or self.test_mode:
                    mock_market_state = {"NIFTY": state, "BANKNIFTY": state, "SENSEX": state}
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
