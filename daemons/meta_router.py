"""
daemons/meta_router.py
======================
Tri-Brain HMM Dispatcher & Portfolio Allocation (V8.0)

Architecture:
  Main asyncio loop   → Reads Market State and Firestore settings;
                        Delegates to RegimeOrchestrator;
                        Dispatches commands via ZMQ.
  RegimeOrchestrator  → Wrapper managing Providers (HMM, Deterministic, Hybrid).
  Providers           → Logic for regime classification and signal weighting.

"""

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_MAX_PORTFOLIO_HEAT = 0.5   # Maximum combined fractional Kelly across all Tri-Brain assets
MAX_RISK_PER_TRADE         = 0.0     # Default 0.0 to prevent orders until UI configures it
ATR_SL_MULTIPLIER          = 1.0     # Mirrors liquidation_daemon constant for sizing consistency
DEFAULT_HURST_THRESHOLD    = 0.55
DEFAULT_HYBRID_CONFIDENCE  = 0.70
DEFAULT_VPIN_TOXICITY      = 0.82

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics
from core.shm import ShmManager
from core.alerts import send_cloud_alert

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MetaRouter")

class BaseStrategyLogic:
    def __init__(self, asset_id: str, redis_client):
        self.asset_id = asset_id
        self.r = redis_client

    async def calculate_weights(self, state: dict, regime_context: dict) -> dict:
        """
        Hybrid Sizing: Conviction × Volatility (v8.0)

        Step A — ATR Unit (Safety Floor)
        ---------------------------------
        Normalizes the "Cost of Being Wrong" by expressing position size as the number
        of units (lots) where one unit = ₹MAX_RISK_PER_TRADE of rupee loss at the ATR stop:

            unit_size = MAX_RISK_PER_TRADE / (ATR × ATR_SL_MULTIPLIER)

        This ensures that regardless of how wide the stop is, the rupee P&L exposure
        at stop-loss is always ₹2,500 — not a percentage of capital.

        Step B — Kelly Multiplier (Conviction)
        ----------------------------------------
        Half-Kelly fraction scales the unit_size by win-probability conviction:

            final_lots = unit_size × max(0.01, 0.5 × kelly_f)

        where kelly_f = p − ((1 − p) / 1.5)
        """
        try:
            p = float(regime_context.get("prob", 0.6))
            r_type = regime_context.get("regime", "RANGING")
            provider = regime_context.get("provider", "UNKNOWN")

            # ── Step A: ATR Unit ──────────────────────────────────────────────
            try:
                atr = float(await self.r.get("atr") or 20.0) if self.r else 20.0
            except Exception:
                atr = 20.0
            atr = max(atr, 1.0)  # Guard against zero ATR

            # Read MAX_RISK_PER_TRADE from Redis — separate keys for paper vs live
            # Determine mode: live trading is active when LIVE_CAPITAL_LIMIT > 0
            try:
                live_cap = float(await self.r.get("LIVE_CAPITAL_LIMIT") or 0) if self.r else 0
                if live_cap > 0:
                    risk_key = "CONFIG:MAX_RISK_PER_TRADE_LIVE"
                else:
                    risk_key = "CONFIG:MAX_RISK_PER_TRADE_PAPER"
                max_risk = float(await self.r.get(risk_key) or MAX_RISK_PER_TRADE) if self.r else MAX_RISK_PER_TRADE
                max_risk = max(max_risk, 0.0)  # Floor: Allow 0 to prevent trading
            except Exception:
                max_risk = MAX_RISK_PER_TRADE

            unit_size = max_risk / (atr * ATR_SL_MULTIPLIER)

            # ── Step B: Kelly Multiplier ──────────────────────────────────────
            b = 1.5
            kelly_f = p - ((1.0 - p) / b)
            half_kelly = max(0.01, 0.5 * kelly_f)

            final_lots = round(unit_size * half_kelly, 4)

            return {
                "asset": self.asset_id,
                "regime": r_type,
                "lots": final_lots,         # ATR-normalized lot count
                "weight": half_kelly,        # Raw Half-Kelly fraction (used by heat constraint)
                "unit_size": round(unit_size, 4),
                "atr_used": round(atr, 4),
                "provider": provider,
                "score": p
            }
        except Exception as e:
            logger.error(f"Logic failure on {self.asset_id}: {e}")
            return {"asset": self.asset_id, "regime": "RANGING", "lots": 0.01, "weight": 0.01, "provider": "ERROR"}

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
        
        if hurst > state.get("hurst_threshold", 0.55) and adx > 25 and er > 0.6:
            regime = "TRENDING"
            prob = min(0.9, 0.5 + (hurst - 0.5) + (adx - 20)/100)
        elif hurst < (state.get("hurst_threshold", 0.55) - 0.1):
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
        threshold = state.get("hybrid_confidence", 0.70)
        if score > threshold:
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
        try:
            # 1. Engine Mode
            mode = await self._redis.get("CONFIG:REGIME_ENGINE")
            if mode in self.orchestrator.providers:
                if self.orchestrator.active_engine != mode:
                    old_mode = self.orchestrator.active_engine
                    logger.info(f"🔄 Switching Regime Engine: {old_mode} -> {mode}")
                    self.orchestrator.active_engine = mode
                    await send_cloud_alert(
                        f"🔄 HOT-SWAP APPLIED: Regime engine switched from {old_mode} to {mode}.",
                        alert_type="CONFIG"
                    )
            
            # 2. Sizing Constraints
            try:
                self.heat_limit = float(await self._redis.get("CONFIG:MAX_PORTFOLIO_HEAT") or DEFAULT_MAX_PORTFOLIO_HEAT)
                self.hurst_threshold = float(await self._redis.get("CONFIG:HURST_THRESHOLD") or DEFAULT_HURST_THRESHOLD)
                self.hybrid_confidence = float(await self._redis.get("CONFIG:HYBRID_CONFIDENCE") or DEFAULT_HYBRID_CONFIDENCE)
                self.vpin_toxicity = float(await self._redis.get("CONFIG:VPIN_TOXICITY") or DEFAULT_VPIN_TOXICITY)
            except:
                self.heat_limit = DEFAULT_MAX_PORTFOLIO_HEAT
                self.hurst_threshold = DEFAULT_HURST_THRESHOLD
                self.hybrid_confidence = DEFAULT_HYBRID_CONFIDENCE
                self.vpin_toxicity = DEFAULT_VPIN_TOXICITY

        except Exception as e:
            logger.error(f"Sync settings failed: {e}")

    async def _config_update_listener(self):
        """Listens for real-time config updates via Redis Pub/Sub."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe("system_cmd")
        logger.info("MetaRouter listening for system_cmd updates...")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    if data.get("cmd") == "HOT_SWAP_REGIME":
                        logger.info("🔥 Real-time Config Update Received")
                        await self._sync_settings()
                except Exception as e:
                    logger.error(f"Config listener error: {e}")

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
                
            # Inject dynamic parameters into state for providers
            state["hurst_threshold"] = self.hurst_threshold
            state["hybrid_confidence"] = self.hybrid_confidence
            
            active_dec, all_decs = await self.orchestrator.get_decisions(
                asset, state, self._redis, logic
            )
            
            # ── A0: ATM Order Construction ───────────────────────────────────
            # Construct real Shoonya scrip name if trading ATM options
            if active_dec.get("lots", 0) > 0:
                await self._enrich_with_atm_symbol(asset, active_dec)
            
            # Recalculate weights based on the regime
            # (In v8.0, active_dec already contains the weights for the active engine)
            all_commands.append(active_dec)
            attribution_payloads.append({
                "asset": asset,
                "timestamp": datetime.now().isoformat(),
                "decisions": all_decs,
                "active_engine": self.orchestrator.active_engine
            })
            
            # ── Shadow P&L Tracking (What-If) ────────────────────────────────
            await self._record_shadow_signals(asset, all_decs)

        if not self.test_mode:
            # ── A1: Global Portfolio Heat Constraint ───────────────────────────
            total_heat = sum(cmd.get("weight", 0) for cmd in all_commands if isinstance(cmd, dict))
            
            if total_heat > self.heat_limit and total_heat > 0:
                scale = self.heat_limit / total_heat
                all_commands = [
                    {**cmd, "weight": round(cmd.get("weight", 0) * scale, 4)}
                    if isinstance(cmd, dict) else cmd
                    for cmd in all_commands
                ]
                logger.warning(
                    f"🔥 PORTFOLIO HEAT CAP: total_f={total_heat:.3f} > limit={self.heat_limit:.3f}. "
                    f"Scaling all weights by {scale:.3f}."
                )
                await send_cloud_alert(
                    f"🔥 PORTFOLIO HEAT CAP TRIGGERED\n"
                    f"Total Heat: {total_heat:.3f} | Limit: {self.heat_limit:.3f}\n"
                    f"Scaling factor: {scale:.3f} applied to all positions.",
                    alert_type="RISK"
                )
                await self._redis.set("PORTFOLIO_HEAT_CAPPED", "True", ex=60)
            else:
                await self._redis.set("PORTFOLIO_HEAT_CAPPED", "False", ex=60)

            for cmd in all_commands:
                await self.mq.send_json(self.cmd_pub, cmd, topic=cmd["asset"])
            
            # Log attributions for Sidecar to push to Firestore
            await self._redis.set("latest_attributions", json.dumps(attribution_payloads))

    async def _enrich_with_atm_symbol(self, asset: str, decision: dict):
        """Builds scrip name (e.g. NIFTY26MAR22350CE) for ATM options."""
        strikes = await self._redis.hgetall("optimal_strikes")
        side = "CE" if decision.get("lots", 0) > 0 else "PE" # Simple directional bias
        strike = strikes.get(f"{asset}_{side}")
        expiry = await self._redis.get("CURRENT_EXPIRY_DATE") or "26MAR" # Expected from DataGateway
        
        if strike:
            decision["tradingsymbol"] = f"{asset}{expiry}{int(float(strike))}{side}"
            decision["exchange"] = "NFO"
        else:
            decision["tradingsymbol"] = asset # Fallback to index (sim only)

    async def _record_shadow_signals(self, asset: str, all_decs: dict):
        """Persists shadow engine lot sizes for What-If analysis."""
        for engine, dec in all_decs.items():
            key = f"shadow_lots:{engine}:{asset}"
            await self._redis.lpush(key, dec.get("lots", 0))
            await self._redis.ltrim(key, 0, 100) # Keep 100 ticks of history

    async def run(self):
        logger.info("MetaRouter [Core 3] Tri-Brain active. Waiting for market & model convergence...")
        
        # Start config listener in background
        asyncio.create_task(self._config_update_listener())
        
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
