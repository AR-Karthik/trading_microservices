"""
daemons/meta_router.py
======================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Architecture:
  Main asyncio loop   → Orchestrates NIFTY, BANKNIFTY, SENSEX parallel flows.
  RegimeOrchestrator  → Dispatches commands via ZMQ.
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
import uuid
import time
from datetime import datetime, timezone

import redis.asyncio as redis
from core.logger import setup_logger
from core.mq import MQManager, Ports, Topics
from core.shm import ShmManager, SignalVector
from core.alerts import send_cloud_alert
from core.greeks import BlackScholes
import asyncpg
from dotenv import load_dotenv
load_dotenv()

logger = setup_logger("MetaRouter", log_file="logs/meta_router.log")

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
                atr = float(await self.r.get(f"atr:{self.asset_id}") or await self.r.get("atr") or 20.0) if self.r else 20.0
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

            final_lots: float = round(float(unit_size * half_kelly), 4)

            return {
                "asset": self.asset_id,
                "regime": r_type,
                "lots": final_lots,         # ATR-normalized lot count
                "weight": float(half_kelly), # Raw Half-Kelly fraction (used by heat constraint)
                "unit_size": float(unit_size),
                "atr_used": float(atr),
                "provider": provider,
                "score": float(p)
            }
        except Exception as e:
            logger.error(f"Logic failure on {self.asset_id}: {e}")
            return {"asset": self.asset_id, "regime": "RANGING", "lots": 0.01, "weight": 0.01, "provider": "ERROR"}

# ── Providers: The Decision Engines ───────────────────────────────────────────

# ── Phase 1.2: Lifecycle Mapping ─────────────────────────────────────────────
LIFECYCLE_MAP = {
    "GammaScalping": "KINETIC",
    "OIPulse": "KINETIC",
    "AnchoredVWAP": "KINETIC",
    "IronCondor": "POSITIONAL",
    "CreditSpread": "POSITIONAL",
}

class HeuristicRegimeReader:
    """Reads the deterministic regime + heuristics from Redis (written by HeuristicEngine)."""
    async def get_context(self, asset: str, state: dict, redis_client) -> dict:
        raw = await redis_client.hget("hmm_regime_state", asset)
        if not raw:
            return {"regime": "WAITING", "prob": 0.5, "heuristics": {"verdict": "YELLOW"}}
        try:
            return json.loads(raw)
        except:
            return {"regime": "WAITING", "prob": 0.5, "heuristics": {"verdict": "YELLOW"}}

# ── The Regime Orchestrator ───────────────────────────────────────────────────

class RegimeOrchestrator:
    def __init__(self, reader):
        self.reader = reader

    async def get_decisions(self, asset: str, state: dict, redis_client, logic_class):
        """Returns active decision + metadata for attribution."""
        ctx = await self.reader.get_context(asset, state, redis_client)
        decision = await logic_class.calculate_weights(state, ctx)
        return decision, ctx

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

        # Setup Orchestrator (Phase 1.2 Simplified)
        self.orchestrator = RegimeOrchestrator(HeuristicRegimeReader())
        
        # Asset Logic
        self.brains = {
            "NIFTY": BaseStrategyLogic("NIFTY", self._redis if not test_mode else None),
            "BANKNIFTY": BaseStrategyLogic("BANKNIFTY", self._redis if not test_mode else None),
            "SENSEX": BaseStrategyLogic("SENSEX", self._redis if not test_mode else None),
        }

        # Initialize attributes to defaults
        self.heat_limit = DEFAULT_MAX_PORTFOLIO_HEAT
        self.hurst_threshold = DEFAULT_HURST_THRESHOLD
        self.hybrid_confidence = DEFAULT_HYBRID_CONFIDENCE
        self.vpin_toxicity = DEFAULT_VPIN_TOXICITY
        self.net_delta_nifty: float = 0.0
        self.net_delta_banknifty: float = 0.0
        self.last_greek_ts: float = 0.0
    
    async def _sync_settings(self):
        """Syncs engine mode and parameters from Redis (upstream Firestore)."""
        try:
            # 1. Engine Mode (Simplified Phase 1.2)
            # Legacy hot-swap removed to rely on deterministic HeuristicEngine
            pass
            
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

    async def _calculate_portfolio_greeks(self):
        """Phase 5: Calculates net delta across all live positions."""
        try:
            dsn = os.getenv("DB_DSN")
            if not dsn: return
            
            conn = await asyncpg.connect(dsn)
            rows = await conn.fetch("SELECT * FROM portfolio WHERE quantity != 0")
            
            n_delta: float = 0.0
            b_delta: float = 0.0
            
            # Simplified Greek calculation logic
            for row in rows:
                qty = float(row['quantity'] or 0.0)
                und = str(row['underlying'] or "UNKNOWN")
                sym = str(row['symbol'] or "UNKNOWN")
                
                # Basic Delta assignments if not calculating Black-Scholes in-situ
                # In production, we'd pull Spot/IV from Redis for each leg
                if "CE" in sym:
                    leg_delta = 0.5 # Placeholder
                elif "PE" in sym:
                    leg_delta = -0.5 # Placeholder
                else:
                    leg_delta = 1.0 if qty > 0 else -1.0 # Futures/Equity
                
                if und == "NIFTY":
                    n_delta += float(leg_delta * qty)
                elif und == "BANKNIFTY":
                    b_delta += float(leg_delta * qty)
            
            self.net_delta_nifty = n_delta
            self.net_delta_banknifty = b_delta
            
            await conn.close()
            # Export to Redis for UI/Liquidation accessibility
            await self._redis.set("NET_DELTA_NIFTY", f"{n_delta:.2f}")
            await self._redis.set("NET_DELTA_BANKNIFTY", f"{b_delta:.2f}")
            
        except Exception as e:
            logger.error(f"Greek calculation error: {e}")

    async def broadcast_decisions(self, market_state: dict, regimes: dict = None):
        """Formulate execution payloads and attribution logs."""
        if regimes is None: regimes = {}
        all_commands = []
        attribution_payloads = []

        await self._sync_settings()

        is_fractured = await self._check_cross_index_divergence(regimes)

        # 4. Phase 5: Periodic Portfolio Greek Reconciliation
        if time.time() - self.last_greek_ts > 10: # Every 10s
            await self._calculate_portfolio_greeks()
            self.last_greek_ts = time.time()

        for asset, logic in self.brains.items():
            state = market_state.get(asset, {})
            regime = regimes.get(asset, {"regime": "WAITING"})
            
            if is_fractured:
                regime["regime"] = "WAITING" # Veto active
                
            # Inject dynamic parameters into state for providers
            state["hurst_threshold"] = self.hurst_threshold
            state["hybrid_confidence"] = self.hybrid_confidence
            
            active_dec, ctx = await self.orchestrator.get_decisions(
                asset, state, self._redis, logic
            )
            
            # --- Phase 1.2: Payload Tagging ---
            active_dec["parent_uuid"] = f"{asset}_{uuid.uuid4().hex[:8]}"
            # Default to KINETIC if strategy_id not found or missing
            strat_id = str(active_dec.get("strategy_id", "KINETIC"))
            active_dec["lifecycle_class"] = str(LIFECYCLE_MAP.get(strat_id, "KINETIC"))
            
            # Use separate keys to avoid dict-indexing-on-str errors
            audit = {
                "regime": str(ctx.get("regime", "UNKNOWN")),
                "vpin": float(state.get("vpin", 0.0)),
                "heat_limit": float(self.heat_limit)
            }
            active_dec["audit_tags"] = audit
            
            # ── A0: ATM Order Construction ───────────────────────────────────
            if active_dec.get("lots", 0) > 0:
                await self._enrich_with_atm_symbol(asset, active_dec)
            
            all_commands.append(active_dec)
            attribution_payloads.append({
                "asset": asset,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "regime": ctx.get("regime"),
                "active_engine": "HEURISTIC"
            })

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
                asyncio.create_task(send_cloud_alert(
                    f"🔥 PORTFOLIO HEAT CAP TRIGGERED\n"
                    f"Total Heat: {total_heat:.3f} | Limit: {self.heat_limit:.3f}\n"
                    f"Scaling factor: {scale:.3f} applied to all positions.",
                    alert_type="RISK"
                ))
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
        side = "CE" if decision.get("lots", 0) > 0 else "PE" 
        strike = strikes.get(f"{asset}_{side}")
        
        # Use per-asset expiry key
        expiry = await self._redis.get(f"EXPIRY:{asset}")
        if not expiry:
            # Fallback to legacy key for NIFTY or hardcoded default
            expiry = await self._redis.get("CURRENT_EXPIRY_DATE") or "26MAR"
        
        if strike:
            tsym = f"{asset}{expiry}{int(float(strike))}{side}"
            decision["tradingsymbol"] = tsym
            decision["exchange"] = "BFO" if asset == "SENSEX" else "NFO"
            
            # Phase 6: Trigger JIT Subscription if not in simulation
            if not self.test_mode:
                # We need the token for JIT sub, but MetaRouter doesn't have it.
                # DataGateway can resolve the tsym to token via search_scrip if we send the tsym.
                # Let's send "NFO|TSYM" and let DataGateway handle it.
                await self._redis.publish("dynamic_subscriptions", f"{decision['exchange']}|{tsym}")
        else:
            decision["tradingsymbol"] = asset # Fallback to index (sim only)

    async def _record_shadow_signals(self, asset: str, all_decs: dict):
        """Persists shadow engine lot sizes for What-If analysis."""
        for engine, dec in all_decs.items():
            key = f"shadow_lots:{engine}:{asset}"
            await self._redis.lpush(key, dec.get("lots", 0))
            await self._redis.ltrim(key, 0, 100) # Keep 100 ticks of history

    async def run(self):
        logger.info("Project K.A.R.T.H.I.K. Orchestrator Active. Managing NIFTY, BANKNIFTY, SENSEX parallel weights.")
        asyncio.create_task(send_cloud_alert("🚦 PROJECT K.A.R.T.H.I.K.: Orchestrator online.", alert_type="SYSTEM"))
        
        # Start config listener in background
        asyncio.create_task(self._config_update_listener())
        
        # ── Phase 9: UI & Observability heartbeat ──
        from core.health import HeartbeatProvider
        self.hb = HeartbeatProvider("MetaRouter", self._redis)
        asyncio.create_task(self.hb.run_heartbeat())
        
        while True:
            try:
                # Polling Tri-Asset states and regimes
                assets = ["NIFTY50", "BANKNIFTY", "SENSEX"]
                multi_market_state = {}
                
                # Regimes from hash
                regimes_raw = await self._redis.hgetall("hmm_regime_state") if not self.test_mode else {}
                regimes = {k: json.loads(v) for k, v in regimes_raw.items()}

                for asset in assets:
                    # Fetch per-asset state
                    st_raw = await self._redis.get(f"latest_market_state:{asset}")
                    if not st_raw and asset == "NIFTY50":
                        st_raw = await self._redis.get("latest_market_state") # legacy fallback
                    
                    multi_market_state[asset] = json.loads(st_raw) if st_raw else {}

                # Orchestrate if we have any data
                if any(multi_market_state.values()) or self.test_mode:
                    await self.broadcast_decisions(multi_market_state, regimes)
                
                await asyncio.sleep(0.1) 

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
