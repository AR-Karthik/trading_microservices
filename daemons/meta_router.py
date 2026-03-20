import asyncio
import json
import logging
import os
import sys
import time
import uuid
import zmq
import zmq.asyncio

# ── Constants ──────────────────────────────────────────────────────────────────
DEFAULT_MAX_PORTFOLIO_HEAT = 0.25  # [C2-01] Maximum combined fractional Kelly — spec says 25%, was 50%
MAX_RISK_PER_TRADE         = 0.0     # Default 0.0 to prevent orders until UI configures it
ATR_SL_MULTIPLIER          = 1.0     # Mirrors liquidation_daemon constant for sizing consistency
DEFAULT_HURST_THRESHOLD    = 0.55
DEFAULT_HYBRID_CONFIDENCE  = 0.70
DEFAULT_VPIN_TOXICITY      = 0.82
HEDGE_RESERVE_PCT          = 0.15  # [C2-01] 15% of capital reserved for hedging

# [Audit-Fix] LUA Script for atomic margin reservation
LUA_RESERVE_SCRIPT = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local current = tonumber(redis.call('get', key) or '0')
if current >= amount then
    redis.call('set', key, tostring(current - amount))
    return 1
else
    return 0
end
"""

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

import zmq
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
        except Exception:
            return {"regime": "WAITING", "prob": 0.5, "heuristics": {"verdict": "YELLOW"}}  # [R2-22]

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
            from core.auth import get_redis_url
            redis_url = get_redis_url()
            self._redis = redis.from_url(redis_url, decode_responses=True)
            self.shm = ShmManager(mode='r')
            # [Audit-Fix] Register LUA script
            self._lua_reserve = self._redis.register_script(LUA_RESERVE_SCRIPT)
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
        self.all_indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]

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
        self.hedge_reserve: float = 0.0 # [C2-01] Explicit 15% tranche
        self.quarantine_premium: float = 0.0
        
        # Throttling for "Portfolio Heat Map" alerts
        self.last_heat_alert_val: float = 0.0
        self.last_heat_alert_ts: float = 0.0
    
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
                self.vpin_toxicity = float(await self._redis.get("CONFIG:VPIN_TOXICITY_THRESHOLD") or float(os.getenv("VPIN_TOXICITY_THRESHOLD", "0.80")))
                
                # [R2-05] Fixed: use same key names as system_controller writes
                self.cash_component = float(await self._redis.get("CASH_COMPONENT_LIVE") or 1000000.0)
                self.collateral_component = float(await self._redis.get("COLLATERAL_COMPONENT_LIVE") or 1000000.0)
                # [Audit-Fix] Explicitly fetch Hedge Reserve from Redis (set by SystemController)
                self.hedge_reserve = float(await self._redis.get("HEDGE_RESERVE_LIVE") or 0.0)
                self.quarantine_premium = float(await self._redis.get("QUARANTINE_PREMIUM_LIVE") or 0.0)
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
            
            # [R2-03] Use async with to prevent connection leaks
            async with await asyncpg.connect(dsn) as conn:
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
                            try:
                                spot = json.loads(spot_raw).get("price", 0.0)
                            except Exception:
                                spot = 0.0
                            
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

            # [C2-07] ASTO Shield Veto: block Iron Condor entry if |ASTO| > 60
            asto_val = float(state.get("asto", 0.0))
            if abs(asto_val) > 60 and active_dec.get("lifecycle_class") == "POSITIONAL":
                logger.warning(f"🛡️ ASTO SHIELD VETO: |{asto_val:.1f}| > 60. Blocking POSITIONAL entry for {asset}.")
                active_dec["lots"] = 0

            # [C2-09] ASTO Hunter Veto: block KINETIC if |ASTO| <= 70 or ADX <= 25
            adx_val = float(state.get("adx", 0.0))
            if active_dec.get("lifecycle_class") == "KINETIC":
                if abs(asto_val) <= 70 or adx_val <= 25:
                    logger.warning(f"🏹 ASTO HUNTER VETO: |{asto_val:.1f}| <= 70 or ADX {adx_val:.1f} <= 25. Blocking KINETIC entry for {asset}.")
                    active_dec["lots"] = 0

            # [C2-08] ASTO Delta Hedge Trigger: emergency hedge if |ASTO| > 90
            if abs(asto_val) > 90 and active_dec.get("lifecycle_class") == "POSITIONAL":
                logger.critical(f"🛡️ ASTO GIGA-VIX TRIGGERED: |{asto_val:.1f}| > 90. Activating Emergency Hedge for {asset}.")

                # Send out-of-band delta-neutralizing intent to Sidecar/Bridge
                hedge_intent = {
                    "asset": asset,
                    "action": "AUTO_HEDGE",
                    "reason": "ASTO_GIGA_VIX",
                    "parent_uuid": active_dec.get("parent_uuid"),
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }
                await self.mq.send_json(self.cmd_pub, "SYSTEM_CMD", hedge_intent)
            
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
            # --- [Audit-Fix] Component 4: Risk Guards ---
            
            # 1. Low Vol Trap (ATR < 0.15)
            # We check ATR from the signals. If normalized ATR is too low, we veto new positions.
            for cmd in all_commands:
                if isinstance(cmd, dict):
                    # Normalized ATR < 0.15 is considered 'Dead Air'
                    if cmd.get("atr", 1.0) < 0.15:
                        logger.warning(f"⚠️ LOW_VOL_TRAP: {cmd['asset']} ATR < 0.15. Vetoing new size.")
                        cmd["lots"] = 0

            # 2. VIX Spike Veto (> 15% 5m expansion)
            # Global guard set by MarketSensor/SystemController
            vix_spike = await self._redis.get("VIX_SPIKE_DETECTED")
            if vix_spike == "True":
                logger.critical("🚨 VIX_SPIKE_VETO: Rapid volatility expansion pulse. Freezing all entries.")
                for cmd in all_commands:
                    if isinstance(cmd, dict):
                        cmd["lots"] = 0

            # 3. Macro Event Lockdown (Global Guard)
            macro_lockdown = await self._redis.get("MACRO_EVENT_LOCKDOWN")
            if macro_lockdown == "True":
                logger.warning("🔒 MACRO_EVENT_LOCKDOWN: Global freeze active via SysAdmin.")
                for cmd in all_commands:
                    if isinstance(cmd, dict):
                        cmd["lots"] = 0

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
                
                # --- CHANGE-BASED THROTTLE (SRS §10.2 / User Request) ---
                now = time.time()
                heat_change = abs(total_heat - self.last_heat_alert_val)
                time_since_last = now - self.last_heat_alert_ts
                
                # Alert if:
                # 1. First time triggering (val was 0)
                # 2. Significant change in heat (> 0.05)
                # 3. Minimum 5-minute interval even if heat is high
                if self.last_heat_alert_val == 0 or (heat_change > 0.05 and time_since_last > 60) or time_since_last > 300:
                    asyncio.create_task(send_cloud_alert(
                        f"🔥 PORTFOLIO HEAT MAP TRIGGERED\n"
                        f"Total Heat: {total_heat:.3f} | Limit: {self.heat_limit:.3f}\n"
                        f"Scaling factor: {scale:.3f} applied to all positions.",
                        alert_type="RISK"
                    ))
                    self.last_heat_alert_val = total_heat
                    self.last_heat_alert_ts = now
                else:
                    if int(now) % 10 == 0: # Log every ~10s to avoid log spam
                        logger.info(f"🔇 Throttling Portfolio Heat Alert: change={heat_change:.4f}, elapsed={time_since_last:.1f}s")
                
                await self._redis.set("PORTFOLIO_HEAT_CAPPED", "True", ex=60)
            else:
                if self.last_heat_alert_val > 0:
                    # Reset when heat drops below limit
                    logger.info("🟢 Portfolio heat returned below limit.")
                    self.last_heat_alert_val = 0.0
                await self._redis.set("PORTFOLIO_HEAT_CAPPED", "False", ex=60)

            for cmd in all_commands:
                # [Audit-Fix] Pre-Flight Margin Reservation via LUA
                if cmd.get("lots", 0) > 0:
                    asset = cmd["asset"]
                    # Approximate margin: 1.25L per lot
                    margin_per_lot = 125000.0
                    total_req = cmd["lots"] * margin_per_lot
                    
                    # Try to reserve from CASH_COMPONENT_LIVE
                    # Note: LUA script returns 1 for success, 0 for fail
                    reserved = await self._lua_reserve(keys=["CASH_COMPONENT_LIVE"], args=[str(total_req)])
                    
                    if not reserved:
                        logger.error(f"🛑 ATOMIC MARGIN REJECTION: Insufficient cash for {asset} ({cmd['lots']} lots).")
                        cmd["lots"] = 0
                        continue
                    else:
                        logger.info(f"✅ MARGIN RESERVED: ₹{total_req:.2f} allocated for {asset}.")

                # [R2-04] Fixed: send_json signature is (socket, topic, data)
                await self.mq.send_json(self.cmd_pub, cmd["asset"], cmd)
            
            # Log attributions for Sidecar to push to Firestore
            await self._redis.set("latest_attributions", json.dumps(attribution_payloads))

    async def _build_basket_intent(self, asset: str, decision: dict):
        """Constructs multi-leg payloads for IronCondor or DirectionalCredit."""
        strat = decision.get("strategy_id")
        spot = decision.get("audit_tags", {}).get("spot", 0.0)
        if spot == 0:
             # Try to fetch spot from state if not in audit_tags
             spot_raw = await self._redis.get(f"latest_market_state:{asset}")
             # [R2-18] Guard against corrupted JSON
             try:
                 spot = json.loads(spot_raw).get("price", 0.0) if spot_raw else 20000.0
             except (json.JSONDecodeError, TypeError):
                 spot = 20000.0

        expiry = await self._redis.get(f"EXPIRY:{asset}") or "26MAR"
        exch = "BFO" if asset == "SENSEX" else "NFO"
        
        legs = []
        
        # Helper to find strike for target delta
        async def find_strike(target_delta: float, otype: str) -> float:
            # [MR-01] Generalize strike rounding based on asset
            increment = 100 if asset in ["BANKNIFTY", "SENSEX"] else 50
            base = round(spot / increment) * increment
            best_s = base
            min_diff = 1.0
            r = 0.07 # Risk-free rate
            iv = 0.18 # Default IV
            t = 2.0 / 365 # 2 days to expiry
            # Search 20 strikes above and below
            for offset in range(-increment * 20, increment * 21, increment):
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
        
        # [Audit-Fix] Propagate parent_uuid to all legs for Execution Bridge tracking
        p_uuid = decision.get("parent_uuid", "NONE")
        for leg in legs:
            leg["parent_uuid"] = p_uuid
            leg["asset"] = asset
            leg["strategy_id"] = strat
            leg["execution_type"] = decision.get("execution_type", "Actual")
        
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

    async def _reboot_recovery(self):
        """[Audit-Fix] Recovers partial payloads from Pending_Journal on boot."""
        if not self._redis: return
        logger.info("MetaRouter: Scanning Pending_Journal for boot-time recovery...")
        try:
            keys = await self._redis.keys("Pending_Journal:*")
            for k in keys:
                raw = await self._redis.get(k)
                if raw:
                    payload = json.loads(raw)
                    logger.warning(f"RECOVEY: Found orphaned intent {k}. Re-dispatching to SYSTEM_CMD.")
                    # Re-dispatch for Reconciler/Bridge to pickup
                    await self.mq.send_json(self.cmd_pub, payload["asset"], payload)
        except Exception as e:
            logger.error(f"Reboot recovery failed: {e}")

    async def _hedge_request_listener(self):
        """Listens for HEDGE_REQUESTs from Strategy Engine."""
        sub = self.mq.create_pull(Ports.HEDGE_REQUEST, bind=True) # MetaRouter binds, StrategyEngine connects
        # Note: In bridge, Ports.ORDERS is PULL. We need MetaRouter to PUSH to Bridge.
        # But Bridge is already BINDING to Ports.ORDERS? 
        # Actually, let's check mq.py: PULL usually binds=False by default.
        # Wait, LiveBridge binds to Ports.ORDERS. So MetaRouter should CONNECT to it.
        push_orders = self.mq.create_push(Ports.ORDERS, bind=False)
        push_orders.connect(f"tcp://{self.mq.mq_hosts['orders']}:{Ports.ORDERS}")

        logger.info("MetaRouter: HEDGE_REQUEST listener active.")
        while True:
            try:
                topic, req = await self.mq.recv_json(sub)
                if not req: continue
                
                asset = req.get("symbol", "").split("-")[0]
                asto = float(req.get("asto", 0.0))
                
                # 1. Check MARKET_STATE (Deterministic Model)
                m_state = await self._redis.get("MARKET_STATE") or "NEUTRAL"
                if "EXTREME_TREND" not in m_state:
                    logger.warning(f"🛡️ HEDGE VETO: Market State is {m_state}, not EXTREME_TREND. Ignoring hedge for {asset}.")
                    continue
                
                # 2. 15% Reserve Check
                total_bp = self.cash_component + self.collateral_component
                hedge_limit = total_bp * HEDGE_RESERVE_PCT
                current_hedge_usage = float(await self._redis.get(f"HEDGE_USAGE:{asset}") or 0.0)
                
                # Estimate cost for Micro-Future margin (e.g. 5k INR)
                cost = float(req.get("quantity", 1)) * 5000.0 
                
                if (current_hedge_usage + cost) > hedge_limit:
                    logger.critical(f"🛑 HEDGE MARGIN VETO: Asset {asset} exceeded 15% hedge reserve limit.")
                    continue
                
                # 3. Dispatch Micro-Futures Order
                # Ensure parent_uuid is linked for "Hybrid" tracking
                order = {
                    "order_id": str(uuid.uuid4()),
                    "symbol": req["symbol"],
                    "action": req["side"], # BUY/SELL
                    "quantity": req["quantity"],
                    "order_type": "MARKET",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "strategy_id": "HEDGE_HYBRID",
                    "execution_type": "Actual", # Or req.get("execution_type")
                    "lifecycle_class": "KINETIC", 
                    "parent_uuid": req.get("parent_uuid"),
                    "audit_tags": {
                        "hedge_label": req.get("hedge_label"),
                        "reason": req.get("reason"),
                        "m_state": m_state
                    }
                }
                
                # Atomic Reserve
                reserved = await self._lua_reserve(keys=["CASH_COMPONENT_LIVE"], args=[str(cost)])
                if reserved:
                    await self.mq.send_json(push_orders, Topics.ORDER_INTENT, order)
                    await self._redis.incrbyfloat(f"HEDGE_USAGE:{asset}", cost)
                    logger.info(f"🚀 HEDGE DISPATCHED: {req['symbol']} {req['side']} Qty={req['quantity']} | Parent: {order['parent_uuid']}")
                else:
                    logger.error(f"🛑 HEDGE RESERVATION FAIL: ₹{cost} rejected by LUA.")

            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Hedge listener error: {e}")
                await asyncio.sleep(1)

    async def run(self):
        logger.info("Project K.A.R.T.H.I.K. Orchestrator Active. Managing NIFTY, BANKNIFTY, SENSEX parallel weights.")
        asyncio.create_task(send_cloud_alert("🚦 PROJECT K.A.R.T.H.I.K.: Orchestrator online.", alert_type="SYSTEM"))
        
        # [Audit-Fix] Boot-time recovery of pending journals
        await self._reboot_recovery()

        # Start listeners
        asyncio.create_task(self._config_update_listener())
        asyncio.create_task(self._hedge_request_listener())
        
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

                # Orchestrate if we have any data - [Wave 4.2] Filter to indices only for broadcast
                if any(multi_market_state.values()) or self.test_mode:
                    filtered_state = {k: v for k, v in multi_market_state.items() if k in self.all_indices}
                    await self.broadcast_decisions(filtered_state, regimes)
                
                # --- [IPC Health Audit] Check SHM Fallback ---
                if not self.test_mode:
                    try:
                        shm_data = self.shm.read()
                        if shm_data:
                            await self._redis.set("SHM_FALLBACK:ACTIVE", "False")
                        else:
                            await self._redis.set("SHM_FALLBACK:ACTIVE", "True")
                            logger.warning("⚠️ IPC ALERT: SHM returned empty data. Fallback active.")
                    except Exception as e:
                        await self._redis.set("SHM_FALLBACK:ACTIVE", "True")
                        logger.error(f"🚨 IPC FAILURE: SHM Read Error: {e}")
                
                await asyncio.sleep(0.5)  # [R2-19] 100ms→500ms to reduce Redis pressure

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
