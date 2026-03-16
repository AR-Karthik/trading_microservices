"""
daemons/meta_router.py
======================
Project K.A.R.T.H.I.K. (Kinetic Algorithmic Real-Time High-Intensity Knight)

Architecture:
  Main asyncio loop   → Orchestrates NIFTY, BANKNIFTY, SENSEX parallel flows.
  RegimeOrchestrator  → Dispatches commands via ZMQ.
"""

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_MAX_PORTFOLIO_HEAT = 0.25  # [C2-01] Maximum combined fractional Kelly — spec says 25%, was 50%
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
import re
from datetime import datetime, timezone

try:
    import uvloop
except ImportError:
    uvloop = None

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
            
            # [Audit 9.7] Asset-specific ATR floors
            atr_floors = {
                "NIFTY50": 20.0,
                "BANKNIFTY": 50.0,
                "SENSEX": 50.0
            }
            atr = max(atr, atr_floors.get(self.asset_id, 10.0))  # Guard against zero/tiny ATR

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
    "InstitutionalFade": "KINETIC",
    "IronCondor": "POSITIONAL",
    "DirectionalCredit": "POSITIONAL",
    "TastyTrade0DTE": "ZERO_DTE",
}

# [C2-03] Regime-Strategy Lock: each strategy is only authorized in these regimes
REGIME_STRATEGY_LOCK = {
    "IronCondor": ["HIGH_VOL_CHOP", "RANGING"],
    "GammaScalping": ["TRENDING"],
    "DirectionalCredit": ["TRENDING"],
    "TastyTrade0DTE": ["RANGING", "HIGH_VOL_CHOP"],
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
            # [Audit 2.1] MetaRouter is the primary binder for SYSTEM_CMD (PUB)
            self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD, bind=True)
            redis_host = os.getenv("REDIS_HOST", "localhost")
            self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
            self.shm = ShmManager(mode='r')
        else:
            self.shm = None

        # Setup Orchestrator (Phase 1.2 Simplified)
        self.orchestrator = RegimeOrchestrator(HeuristicRegimeReader())
        
        # Asset Logic [Audit 3.1: Standardize NIFTY to NIFTY50]
        self.brains = {
            "NIFTY50": BaseStrategyLogic("NIFTY50", self._redis if not test_mode else None),
            "BANKNIFTY": BaseStrategyLogic("BANKNIFTY", self._redis if not test_mode else None),
            "SENSEX": BaseStrategyLogic("SENSEX", self._redis if not test_mode else None),
            # Power 10 Heavyweights
            "RELIANCE": BaseStrategyLogic("RELIANCE", self._redis if not test_mode else None),
            "HDFCBANK": BaseStrategyLogic("HDFCBANK", self._redis if not test_mode else None),
            "ICICIBANK": BaseStrategyLogic("ICICIBANK", self._redis if not test_mode else None),
            "INFY": BaseStrategyLogic("INFY", self._redis if not test_mode else None),
            "TCS": BaseStrategyLogic("TCS", self._redis if not test_mode else None),
            "ITC": BaseStrategyLogic("ITC", self._redis if not test_mode else None),
            "SBIN": BaseStrategyLogic("SBIN", self._redis if not test_mode else None),
            "AXISBANK": BaseStrategyLogic("AXISBANK", self._redis if not test_mode else None),
            "KOTAKBANK": BaseStrategyLogic("KOTAKBANK", self._redis if not test_mode else None),
            "LT": BaseStrategyLogic("LT", self._redis if not test_mode else None),
        }

        # Initialize attributes to defaults
        self.heat_limit = DEFAULT_MAX_PORTFOLIO_HEAT
        self.hurst_threshold = DEFAULT_HURST_THRESHOLD
        self.hybrid_confidence = DEFAULT_HYBRID_CONFIDENCE
        self.vpin_toxicity = DEFAULT_VPIN_TOXICITY
        self.net_delta_nifty50: float = 0.0
        self.net_delta_banknifty: float = 0.0
        self.net_delta_sensex: float = 0.0  # [D-04] Removed duplicate assignment
        self.last_greek_ts: float = 0.0
        self.cash_component: float = 1000000.0  # Default 10L cash
        self.collateral_component: float = 1000000.0 # Default 10L collateral
        self.quarantine_premium: float = 0.0
    
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
                
                # [Audit 8.3] Premium and Cash/Collateral Sync
                self.cash_component = float(await self._redis.get("ACCOUNT:CASH_COMPONENT") or 1000000.0)
                self.collateral_component = float(await self._redis.get("ACCOUNT:COLLATERAL_COMPONENT") or 1000000.0)
                self.quarantine_premium = float(await self._redis.get("ACCOUNT:QUARANTINE_PREMIUM") or 0.0)
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

    async def _check_cross_index_divergence(self, regimes: dict, market_state: dict = None) -> bool:
        """[C2-04] Returns True if the markets are severely fractured (divergence veto)."""
        trends = [r.get("regime") for r in regimes.values()]
        if "TRENDING" in trends and "CRASH" in trends:
            logger.warning("FRACTURED MARKET VETO: Divergent extreme regimes detected.")
            return True
        
        # [C2-04] Price Divergence > 2% between NIFTY50 and BANKNIFTY
        if market_state:
            nifty_price = float(market_state.get("NIFTY50", {}).get("price", 0) or 0)
            bank_price = float(market_state.get("BANKNIFTY", {}).get("price", 0) or 0)
            if nifty_price > 0 and bank_price > 0:
                # Compute daily returns divergence (simplified as price-level ratio deviation)
                nifty_ret = float(market_state.get("NIFTY50", {}).get("daily_return_pct", 0) or 0)
                bank_ret = float(market_state.get("BANKNIFTY", {}).get("daily_return_pct", 0) or 0)
                if abs(nifty_ret - bank_ret) > 2.0:
                    logger.warning(f"PRICE DIVERGENCE VETO: NIFTY={nifty_ret:.1f}% vs BANK={bank_ret:.1f}%")
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
            s_delta: float = 0.0
            
            # Institutional Greek calculation logic
            for row in rows:
                qty = float(row['quantity'] or 0.0)
                und = str(row['underlying'] or "UNKNOWN")
                tsym = str(row['symbol'] or "UNKNOWN")
                
                # Regex to extract Strike/Type: e.g. NIFTY26MAR22350CE
                match = re.search(r"(\d+)(CE|PE)$", tsym)
                if match:
                    strike = float(match.group(1))
                    opt_type = match.group(2)
                    
                    # Fetch real-time market inputs from Redis
                    spot_raw = await self._redis.get(f"latest_market_state:{und}")
                    spot = 0.0
                    if spot_raw:
                        spot = json.loads(spot_raw).get("price", 0.0)
                        
                    iv = float(await self._redis.get(f"iv:{tsym}") or await self._redis.get(f"iv:{und}") or 0.15)
                    dt_exp = await self._redis.get(f"EXPIRY_DT:{und}") or datetime.now().strftime("%Y-%m-%d")
                    t_days = (datetime.strptime(dt_exp, "%Y-%m-%d") - datetime.now()).days
                    t_years = max(t_days, 1) / 365.0
                    
                    leg_delta = BlackScholes.delta(spot, strike, t_years, 0.07, iv, option_type=opt_type.lower())
                else:
                    # Futures or Equity Delta
                    leg_delta = 1.0 if qty > 0 else -1.0
                
                if und == "NIFTY50": n_delta += (leg_delta * qty)
                elif und == "BANKNIFTY": b_delta += (leg_delta * qty)
                elif und == "SENSEX": s_delta += (leg_delta * qty)
            
            self.net_delta_nifty50 = n_delta
            self.net_delta_banknifty = b_delta
            self.net_delta_sensex = s_delta
            
            await conn.close()
            # Export to Redis for UI/Liquidation accessibility
            await self._redis.set("NET_DELTA_NIFTY50", f"{n_delta:.2f}")
            await self._redis.set("NET_DELTA_BANKNIFTY", f"{b_delta:.2f}")
            await self._redis.set("NET_DELTA_SENSEX", f"{s_delta:.2f}")
            
        except Exception as e:
            logger.error(f"Greek calculation error: {e}")

    async def broadcast_decisions(self, market_state: dict, regimes: dict = None):
        """Formulate execution payloads and attribution logs."""
        if regimes is None: regimes = {}
        all_commands = []
        attribution_payloads = []

        await self._sync_settings()

        is_fractured = await self._check_cross_index_divergence(regimes, market_state)

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
            # [Audit 13.1] Dynamic Lifecycle Mapping from Redis
            raw_map = await self._redis.get("CONFIG:LIFECYCLE_MAP") if self._redis else None
            effective_map = json.loads(raw_map) if raw_map else LIFECYCLE_MAP
            active_dec["lifecycle_class"] = str(effective_map.get(strat_id, "KINETIC"))

            # [C2-03] Regime-Strategy Lock: block dispatch if regime not authorized
            allowed_regimes = REGIME_STRATEGY_LOCK.get(strat_id, [])
            current_regime = ctx.get("regime", "WAITING")
            if allowed_regimes and current_regime not in allowed_regimes:
                logger.info(f"REGIME_LOCK: {strat_id} blocked in {current_regime} (allowed: {allowed_regimes})")
                active_dec["lots"] = 0

            # [C2-02] VPIN Toxicity Veto for POSITIONAL entries
            vpin_val = float(state.get("vpin", 0.0))
            if vpin_val > self.vpin_toxicity and active_dec.get("lifecycle_class") == "POSITIONAL":
                logger.warning(f"🛑 VPIN VETO: {vpin_val:.2f} > {self.vpin_toxicity}. Blocking POSITIONAL for {asset}.")
                active_dec["lots"] = 0

            # [V-02] Low Vol Trap Veto: block POSITIONAL if IV < 12%
            iv_val = float(await self._redis.get(f"LIVE_IV:{asset}") or 0.15) if self._redis else 0.15
            if iv_val < 0.12 and active_dec.get("lifecycle_class") == "POSITIONAL":
                logger.warning(f"🛑 LOW_VOL_TRAP: IV {iv_val:.2%} < 12%. Blocking POSITIONAL for {asset}.")
                active_dec["lots"] = 0
            
            # Use separate keys to avoid dict-indexing-on-str errors
            audit = {
                "regime": str(ctx.get("regime", "UNKNOWN")),
                "vpin": float(state.get("vpin", 0.0)),
                "heat_limit": float(self.heat_limit)
            }
            active_dec["audit_tags"] = audit
            
            # ── A0: Order Construction (Single vs Basket) ───────────────────
            if active_dec.get("lots", 0) > 0:
                if active_dec["lifecycle_class"] == "POSITIONAL":
                    await self._build_basket_intent(asset, active_dec)
                else:
                    await self._enrich_with_atm_symbol(asset, active_dec)
            
            all_commands.append(active_dec)
            attribution_payloads.append({
                "asset": asset,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "regime": ctx.get("regime"),
                "active_engine": "HEURISTIC"
            })

        # ── A0.5: Regulatory & Safety Vetoes ──────────────────────────────────
        # [Audit 8.3] 50:50 Cash-to-Collateral validation
        total_margin_required = sum(cmd.get("lots", 0) * 100000 for cmd in all_commands) # Approx margin
        if self.cash_component < (total_margin_required * 0.5):
            logger.critical(f"🛑 REGULATORY VETO: Cash {self.cash_component} < 50% of Margin {total_margin_required}")
            all_commands = [] # Block all new entries
            asyncio.create_task(send_cloud_alert("🛑 SEBI VETO: Cash-to-Collateral Ratio Breach.", alert_type="CRITICAL"))

        # [Audit 15.1] Premium Quarantine Veto (Intraday credits locked)
        if self.quarantine_premium > 0:
            logger.warning(f"🛡️ PREMIUM QUARANTINE ACTIVE: ₹{self.quarantine_premium} credits excluded from BP.")
            # [C2-06] Reduce effective margin: inflight capital excludes unsettled premium
            for cmd in all_commands:
                if isinstance(cmd, dict) and cmd.get("lots", 0) > 0:
                    # Scale down lots proportional to quarantine impact
                    total_bp = self.cash_component + self.collateral_component
                    if total_bp > 0:
                        effective_bp = total_bp - self.quarantine_premium
                        scale = max(0, effective_bp / total_bp)
                        cmd["lots"] = round(cmd["lots"] * scale, 4)

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

    async def _build_basket_intent(self, asset: str, decision: dict):
        """Constructs multi-leg payloads for IronCondor or DirectionalCredit."""
        strat = decision.get("strategy_id")
        spot = decision.get("audit_tags", {}).get("spot", 0.0)
        if spot == 0:
             # Try to fetch spot from state if not in audit_tags
             spot_raw = await self._redis.get(f"latest_market_state:{asset}")
             spot = json.loads(spot_raw).get("price", 0.0) if spot_raw else 20000.0

        expiry = await self._redis.get(f"EXPIRY:{asset}") or "26MAR"
        exch = "BFO" if asset == "SENSEX" else "NFO"
        
        legs = []
        
        # Helper to find strike for target delta
        async def find_strike(target_delta: float, otype: str) -> float:
            base = round(spot / 50) * 50
            best_s = base
            min_diff = 1.0
            r = 0.07 # Risk-free rate
            iv = 0.18 # Default IV
            t = 2.0 / 365 # 2 days to expiry
            for offset in range(-1000, 1050, 50):
                s = base + offset
                d = abs(BlackScholes.delta(spot, s, t, r, iv, otype.lower()))
                if abs(d - target_delta) < min_diff:
                    min_diff = abs(d - target_delta)
                    best_s = s
            return float(best_s)

        if strat == "IronCondor":
            # [C2-05] 4 Legs: Short Put/Call at 0.15 delta, Long wings at 0.05 delta
            sp = await find_strike(0.15, "PE")   # was 0.25
            lp = await find_strike(0.05, "PE")
            sc = await find_strike(0.15, "CE")   # was 0.25
            lc = await find_strike(0.05, "CE")
            
            legs = [
                {"symbol": f"{asset}{expiry}{int(sp)}PE", "action": "SELL", "ratio": 1},
                {"symbol": f"{asset}{expiry}{int(lp)}PE", "action": "BUY",  "ratio": 1},
                {"symbol": f"{asset}{expiry}{int(sc)}CE", "action": "SELL", "ratio": 1},
                {"symbol": f"{asset}{expiry}{int(lc)}CE", "action": "BUY",  "ratio": 1}
            ]
            decision["initial_credit"] = 15.0 # Pre-calc or fetch from BS
            
        elif strat == "DirectionalCredit":
            # 2 Legs: Bull Put or Bear Call
            regime = decision.get("audit_tags", {}).get("regime")
            if "TRENDING" in str(regime) or "UP" in str(regime):
                # Bull Put Spread
                sp = await find_strike(0.25, "PE")
                lp = await find_strike(0.05, "PE")
                legs = [
                    {"symbol": f"{asset}{expiry}{int(sp)}PE", "action": "SELL", "ratio": 1},
                    {"symbol": f"{asset}{expiry}{int(lp)}PE", "action": "BUY",  "ratio": 1}
                ]
            else:
                # Bear Call Spread
                sc = await find_strike(0.25, "CE")
                lc = await find_strike(0.05, "CE")
                legs = [
                    {"symbol": f"{asset}{expiry}{int(sc)}CE", "action": "SELL", "ratio": 1},
                    {"symbol": f"{asset}{expiry}{int(lc)}CE", "action": "BUY",  "ratio": 1}
                ]
            decision["initial_credit"] = 10.0

        decision["legs"] = legs
        decision["exchange"] = exch
        
        # JIT Dynamic Subscription for all legs
        for leg in legs:
            await self._redis.publish("dynamic_subscriptions", f"{exch}|{leg['symbol']}")

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
            
            # JIT Subscription for single-leg ATM
            if not self.test_mode:
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
                # Polling 13-Asset states and regimes
                assets = list(self.brains.keys())
                multi_market_state = {}
                
                # Regimes from hash
                regimes_raw = await self._redis.hgetall("hmm_regime_state") if not self.test_mode else {}
                regimes = {k: json.loads(v) for k, v in regimes_raw.items()}

                for asset in assets:
                    # Fetch per-asset state
                    st_raw = await self._redis.get(f"latest_market_state:{asset}")
                    multi_market_state[asset] = json.loads(st_raw) if st_raw else {}

                # Orchestrate if we have any data
                if any(multi_market_state.values()) or self.test_mode:
                    await self.broadcast_decisions(multi_market_state, regimes)
                
                await asyncio.sleep(0.1) 

            except Exception as e:
                logger.error(f"Router Exception: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    if uvloop:
        uvloop.install()
    elif sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    # Core Pinning happens via taskset wrapper in shell or python PSUtil
    # Since this is the orchestrator, it runs on Core 3
    router = MetaRouter()
    asyncio.run(router.run())
