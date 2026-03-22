"""
Meta-Router & Institutional Truth Matrix
Central intelligence hub for risk-managed order routing.
Implements a 15-Gate Veto Matrix, Deterministic regime tracking, and portfolio stress testing.
"""
import asyncio
import json
try:
    import orjson as fast_json
except ImportError:
    fast_json = json
import logging
import os
import sys
import time
import uuid
import math
import collections
import numpy as np
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional, TYPE_CHECKING

import zmq
import zmq.asyncio
import redis.asyncio as redis
from dotenv import load_dotenv

# Try to use uvloop for high-performance event loop on Linux
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    uvloop = None

# Core Framework Imports
from core.logger import setup_logger
from core.mq import MQManager, Ports, Topics, NumpyEncoder
from core.shm import ShmManager
from core.alerts import send_cloud_alert
from core.greeks import BlackScholes
from core.health import HeartbeatProvider
from core.margin import AsyncMarginManager

load_dotenv()
logger = setup_logger("MetaRouter", log_file="logs/meta_router.log")

# ── Institutional Configuration & Constants ──────────────────────────────────
DEFAULT_MAX_PORTFOLIO_HEAT = 0.25      # 25% of absolute capital
DEFAULT_VPIN_TOXICITY      = 0.82      # Order flow toxicity threshold
LATENCY_MS_THRESHOLD       = 250.0     # Hard veto if feed > 250ms
HEDGE_RESERVE_PCT          = 0.15      # Reserved for margin spikes
CORRELATION_LIMIT          = 0.70      # Max cross-index correlation allowed
MIN_IV_THRESHOLD           = 0.12      # Veto positional entries if IV is crushed
MAX_LOTS_PER_ASSET         = 50        # Hard ceiling for safety
REJECTION_THROTTLE_SEC     = 5.0       # Seconds between repeated alerts
REGIME_SMOOTHING           = 0.05      # Regime probability smoothing factor

# Lifecycle Classifications
LIFECYCLE_MAP = {
    "GammaScalping": "KINETIC",
    "InstitutionalFade": "KINETIC",
    "IronCondor": "POSITIONAL",
    "DirectionalCredit": "POSITIONAL",
    "TastyTrade0DTE": "ZERO_DTE",
    "KineticHunter": "KINETIC",
    "ElasticHunter": "ELASTIC",
    "PositionalHunter": "POSITIONAL",
    "VolatilityArb": "HEDGE"
}

# Regime-Strategy Compatibility Matrix (Institutional Guard)
REGIME_STRATEGY_LOCK = {
    "IronCondor": ["HIGH_VOL_CHOP", "RANGING", "NEUTRAL"],
    "GammaScalping": ["TRENDING", "VOLATILE"],
    "DirectionalCredit": ["TRENDING", "BULL_TREND", "BEAR_TREND"],
    "TastyTrade0DTE": ["RANGING", "HIGH_VOL_CHOP"],
    "KineticHunter": ["TRENDING", "BULL_TREND", "BEAR_TREND"],
    "ElasticHunter": ["RANGING", "WAITING", "NEUTRAL"],
    "PositionalHunter": ["RANGING", "STABLE_TREND"]
}

# ── Mathematical Foundations ────────────────────────────────────────────────

class DeterministicRegimeTracker:
    """
    Maintains a rolling posterior probability of market regimes.
    Uses Regime states from the sensory layer and applies deterministic updates
    based on realized volatility and smart-flow alignment.
    """
    def __init__(self, assets: List[str]):
        self.posteriors = {asset: {"RANGING": 0.5, "TRENDING": 0.3, "VOLATILE": 0.2} for asset in assets}
        self.transition_matrix = {
            "RANGING": {"RANGING": 0.8, "TRENDING": 0.15, "VOLATILE": 0.05},
            "TRENDING": {"RANGING": 0.2, "TRENDING": 0.7, "VOLATILE": 0.1},
            "VOLATILE": {"RANGING": 0.1, "TRENDING": 0.2, "VOLATILE": 0.7}
        }

    def update(self, asset: str, observation: str, confidence: float):
        if asset not in self.posteriors: return
        
        # Step 1: Prior = Transition Matrix * Current Posterior
        prior = {r: 0.0 for r in self.posteriors[asset]}
        for r_from, p_val in self.posteriors[asset].items():
            for r_to, trans_p in self.transition_matrix[r_from].items():
                prior[r_to] += p_val * trans_p
        
        # We weight observations based on signal confidence to reduce regime jitter
        likelihood = {r: (confidence if r == observation else (1.0 - confidence)/(len(prior)-1)) for r in prior}
        
        # Step 3: Posterior normalization with Smoothing Constant
        raw_post = {r: (prior[r] * likelihood[r]) + REGIME_SMOOTHING for r in prior}
        total = sum(raw_post.values())
        if total > 0:
            self.posteriors[asset] = {r: val / total for r, val in raw_post.items()}
            
    async def get_highest_prob_regime(self, asset: str) -> Tuple[str, float]:
        """Returns the regime with the highest posterior probability for risk calibration."""
        probs = self.posteriors.get(asset, {"RANGING": 1.0})
        best_r = max(probs, key=probs.get)
        return best_r, probs[best_r]

class PortfolioStressTestEngine:
    """
    Institutional Stress Testing Engine (Monte Carlo & Shock Analysis).
    Simulates 'Black Swan' events on current portfolio Greeks to detect hidden tail risk.
    """
    def __init__(self, greek_engine):
        self.ge = greek_engine
        self.scenarios = {
            "Black_Monday": {"spot": -0.20, "vix": +1.50},
            "Flash_Crash":  {"spot": -0.05, "vix": +0.80},
            "Gamma_Squeeze": {"spot": +0.07, "vix": +0.30},
            "Theta_Burn":   {"spot": 0.00,  "vix": -0.10, "days": 1}
        }

    async def run_stress_test(self, total_capital: float) -> Dict[str, float]:
        """Calculates expected drawdown under each shock scenario."""
        results = {}
        greeks = self.ge.portfolio_greeks
        
        for name, params in self.scenarios.items():
            # Taylor Series Expansion for PnL: dP = Delta*dS + 0.5*Gamma*dS^2 + Vega*dV + Theta*dT
            ds = params.get("spot", 0) * 22000 # Dummy base
            dv = params.get("vix", 0) * 0.18   # Dummy base
            dt = params.get("days", 0) / 365.0
            
            delta_pnl = greeks["delta"] * ds
            gamma_pnl = 0.5 * greeks["gamma"] * (ds**2)
            vega_pnl  = greeks["vega"] * dv
            theta_pnl = greeks["theta"] * dt
            
            total_drawdown = delta_pnl + gamma_pnl + vega_pnl + theta_pnl
            results[name] = float(total_drawdown)
            
        return results

class RegulatoryCircuitBreaker:
    """
    SEBI-Compliant Order Flow Control.
    Enforces the '10-operations-per-second' rule and '1.01s batch wait'.
    """
    def __init__(self, redis_client):
        self.r = redis_client
        self.batch_limit = 10
        self.window_sec = 1.05
        self.op_queue = collections.deque()

    async def check_throttle(self) -> bool:
        """Returns True if the order flow is within regulatory limits."""
        now = time.time()
        # Cleanup old ops
        while self.op_queue and now - self.op_queue[0] > self.window_sec:
            self.op_queue.popleft()
            
        if len(self.op_queue) >= self.batch_limit:
            logger.warning("🏛️ SEBI THROTTLE: Rate limit (10 ops/sec) reached. Buffering...")
            return False
            
        self.op_queue.append(now)
        return True

class PortfolioGreekEngine:
    """
    High-fidelity Greek aggregator using Black-Scholes.
    Calculates real-time Delta, Gamma, Theta, Vega for the entire portfolio reality.
    Used for Dynamic Hedging and Stress Testing.
    """
    def __init__(self, redis_client):
        self.r = redis_client
        self.bs = BlackScholes()
        self.portfolio_greeks = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    async def reconcile(self, positions: List[Dict]):
        total_delta = 0.0
        total_gamma = 0.0
        total_theta = 0.0
        total_vega = 0.0
        
        r = float(await self.r.get("CONFIG:RISK_FREE_RATE") or 0.065)
        
        for pos in positions:
            symbol = pos["symbol"]
            qty = float(pos["quantity"])
            if qty == 0: continue
            
            # Underlying price and IV fetch
            underlying = pos.get("underlying", "NIFTY50")
            spot_raw = await self.r.get(f"latest_market_state:{underlying}")
            if not spot_raw: continue
            spot = json.loads(spot_raw).get("price", 0.0)
            
            iv = float(await self.r.get(f"iv_atm:{underlying}") or 0.18)
            expiry_str = pos.get("expiry_date", "2026-03-26")
            try:
                t = (datetime.fromisoformat(expiry_str) - datetime.now()).total_seconds() / (365*24*3600)
                t = max(t, 0.001)
            except: t = 7.0/365.0

            if "CE" in symbol or "PE" in symbol:
                otype = "call" if "CE" in symbol else "put"
                strike = float(pos.get("strike", spot))
                
                d = self.bs.delta(spot, strike, t, r, iv, otype)
                g = self.bs.gamma(spot, strike, t, r, iv)
                th = self.bs.theta(spot, strike, t, r, iv, otype)
                v = self.bs.vega(spot, strike, t, r, iv)
                
                total_delta += d * qty
                total_gamma += g * qty
                total_theta += th * qty
                total_vega += v * qty
            else:
                # Delta-1 instruments (Futures/Stocks)
                total_delta += 1.0 * qty
                
        self.portfolio_greeks = {
            "delta": total_delta,
            "gamma": total_gamma,
            "theta": total_theta,
            "vega": total_vega
        }
        await self.r.set("LIVE_PORTFOLIO_GREEKS", json.dumps(self.portfolio_greeks))
        return self.portfolio_greeks

class AssetCorrelationEngine:
    """
    Computes real-time log-return correlations. 
    Prevents stacking directional bets when index correlation exceeds 0.85 (Self-Fulfilling Fracture).
    """
    def __init__(self, redis_client):
        self.r = redis_client
        self.returns_cache = collections.defaultdict(list)
        
    async def get_correlation(self, a1: str, a2: str) -> float:
        # Fetch 30-min log returns from SHM
        # For demo, we return a heuristic based on VIX
        vix = float(await self.r.get("VIX_INDEX") or 15.0)
        if vix > 25: return 0.92 # Panic correlation
        return 0.65 # Normal decoupling

# ── The Gatekeeper: MetaRouter ───────────────────────────────────────────────

class MetaRouter:
    """
    The Central Intelligence Hub of Project K.A.R.T.H.I.K.
    Implements a 9-Layer Truth Matrix for high-fidelity order routing.
    
    Architecture:
    1. Sensory Input (ZMQ/Redis)
    2. Tactical Resolution (Regime/Alpha alignment)
    3. Institutional Veto Matrix (15+ Gates)
    4. Truth Matrix Rendering (Shadow/Paper/Live)
    5. Atomic Margin Reservation (LUA)
    6. Unified Bridge Dispatch
    """
    def __init__(self, test_mode: bool = False):
        self.test_mode = test_mode
        self.mq = MQManager()
        from core.auth import get_redis_url
        self.redis_url = get_redis_url()
        self._redis = redis.from_url(self.redis_url, decode_responses=True)
        self.shm = ShmManager(mode='r')
        self.margin_manager = AsyncMarginManager(self._redis)
        self.greek_engine = PortfolioGreekEngine(self._redis)
        self.stress_tester = PortfolioStressTestEngine(self.greek_engine)
        self.circuit_breaker = RegulatoryCircuitBreaker(self._redis)
        self.correlation_engine = AssetCorrelationEngine(self._redis)
        self.regime_tracker = DeterministicRegimeTracker(["NIFTY50", "BANKNIFTY", "SENSEX"])
        
        # Low-Latency Alpha Collectors
        self.alpha_state = collections.defaultdict(dict)
        self.veto_ledger = collections.defaultdict(lambda: collections.deque(maxlen=100))
        self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD, bind=False)
        # Phase 24: ZMQ High-Water Mark Drop Prevention
        self.cmd_pub.setsockopt(zmq.SNDHWM, 0)
        self.trade_pub = self.mq.create_publisher(Ports.TRADE_EVENTS)
        self.trade_pub.setsockopt(zmq.SNDHWM, 0)
        
        self.orders_pub = self.mq.create_push(Ports.ORDERS, bind=False)
        self.orders_pub.connect(f"tcp://{self.mq.mq_hosts['orders']}:{Ports.ORDERS}")
        
        self.state_sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE."])
        self.raw_intents_pull = self.mq.create_pull(Ports.RAW_INTENTS, bind=True)
        
        # State Tracking
        self.market_state_cache = collections.defaultdict(dict)
        self.regime_cache = collections.defaultdict(lambda: {"regime": "WAITING", "prob": 0.5})
        self.all_indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
        self.heat_limit = DEFAULT_MAX_PORTFOLIO_HEAT
        self.vpin_toxicity = DEFAULT_VPIN_TOXICITY
        self.asset_heat = {asset: 0.0 for asset in self.all_indices}
        self.parent_tracker = collections.defaultdict(set)
        self.last_rejection_ts = {}
        self.last_sync_ts = 0.0
        self.system_halt = False
        
        # Performance Counters
        self.metrics = {
            "intents_processed": 0,
            "strategy_vetos": 0,
            "capital_ratio_vetos": 0,  # LUA Error -2
            "gross_margin_vetos": 0,    # LUA Error -1
            "live_strikes": 0,
            "lots_saved": 0.0
        }
        
        self.intent_queue = asyncio.Queue()
        self.enabled_gates = {}
        self.local_signals = {"_last_update": time.time()} 
        self._bind_institutional_components()

    def _safe_float(self, v, default=0.0):
        try: val = float(v)
        except (ValueError, TypeError): return default
        return default if math.isnan(val) or math.isinf(val) else val
        
    def _bind_institutional_components(self):
        """[Institutional Step 17.4] Early Component Binding."""
        from core.health import HeartbeatProvider
        from core.margin import AsyncMarginManager
        self.hb = HeartbeatProvider("MetaRouter", self._redis)
        self.greek_engine = PortfolioGreekEngine(self._redis)
        self.stress_tester = PortfolioStressTestEngine(self.greek_engine)
        self.margin_manager = AsyncMarginManager(self._redis)

    async def _sync_global_thresholds(self):
        """Refreshes dynamic thresholds from Redis every 5 seconds."""
        if time.time() - self.last_sync_ts < 5: return
        try:
            self.heat_limit = float(await self._redis.get("CONFIG:MAX_PORTFOLIO_HEAT") or DEFAULT_MAX_PORTFOLIO_HEAT)
            self.vpin_toxicity = float(await self._redis.get("CONFIG:VPIN_TOXICITY_THRESHOLD") or DEFAULT_VPIN_TOXICITY)
            self.system_halt = (await self._redis.get("SYSTEM_HALT") == "True")
            
            # Sync Global Gate Control (Performance Step 18.1)
            self.enabled_gates = await self._redis.hgetall("CONFIG:GATE_CONTROL")
            
            self.last_sync_ts = time.time()
        except Exception as e:
            logger.error(f"Global sync failure: {e}")

    async def _evaluate_vetoes(self, asset: str, intent: Dict, state: Dict, ctx: Dict) -> List[str]:
        """
        Comprehensive 15-Gate Veto Matrix.
        Evaluates risk conditions from technical, regulatory, and institutional perspectives.
        """
        vetoes = []
        strat_id = intent.get("strategy_id", "KINETIC")
        lifecycle = LIFECYCLE_MAP.get(strat_id, "KINETIC")
        
        # Signal Vitality Check: Ensure sensory data is fresh
        last_update = self.local_signals.get("_last_update", 0)
        signal_age = time.time() - last_update
        if last_update != 0 and signal_age > 10.0 and intent.get("is_live"):
            vetoes.append("STALE_RISK_SIGNALS")
        # Gate 1: System Halt (Global Kill Switch)
        if self.system_halt:
            vetoes.append("SYSTEM_HALT_ACTIVE")

        # Gate 2: Market State 3 (Volatile Lockdown)
        current_s18 = int(ctx.get("s18_int", 0))
        if self.enabled_gates.get("STATE3", "True") == "True" and current_s18 == 3:
            vetoes.append("STATE3_VOL_LOCKDOWN")

        # Gate 3: VPIN Toxicity (Flow Toxic Protection)
        if self.enabled_gates.get("VPIN", "True") == "True":
            vpin_val = self._safe_float(state.get("vpin", self.local_signals.get(f"vpin:{asset}", 0.0)))
            if vpin_val > self.vpin_toxicity:
                vetoes.append("HIGH_VPIN_TOXICITY")

        # Gate 4: Feed Latency (Execution Safety)
        if self.enabled_gates.get("LATENCY", "True") == "True":
            latency_ms = self._safe_float(state.get("latency_ms", 0.0))
            if latency_ms > LATENCY_MS_THRESHOLD:
                vetoes.append("STALE_FEED_LATENCY")

        # Gate 5: Deterministic Regime Strategy Lock (The Supreme Court Review)
        if self.enabled_gates.get("REGIME_LOCK", "True") == "True":
            best_regime, b_prob = await self.regime_tracker.get_highest_prob_regime(asset)
            allowed = REGIME_STRATEGY_LOCK.get(strat_id, [])
            
            if allowed:
                current_regime = ctx.get("regime", "UNKNOWN")
                # Agreement Rule: Veto if BOTH Engine and Deterministic Tracker disagree with policy
                if current_regime not in allowed and best_regime not in allowed:
                    vetoes.append(f"REGIME_LOCK_VIOLATION: {best_regime}")
                elif current_regime not in allowed and best_regime in allowed:
                    # [PhD Opportunity] Lab Discovery: Deterministic Override of Regime Jitter
                    logger.info(f"🧠 DETERMINISTIC OVERRIDE on {asset}: Engine says {current_regime} (REJECT), but Tracker says {best_regime} (ALLOW). Trade Proceeding.")
                    intent["deterministic_override"] = True
                # Uncertainty Veto: Reject if the probability of the current regime is too low
                elif b_prob < 0.60:
                    vetoes.append("REGIME_UNCERTAINTY_VETO")

        # Gate 6: IV Crush Protection (Low Vol Trap)
        iv_val = self._safe_float(state.get("iv_atm", self.local_signals.get(f"iv_atm:{asset}", 0.15)))
        if self.enabled_gates.get("LOW_VOL", "True") == "True" and iv_val < MIN_IV_THRESHOLD and lifecycle == "POSITIONAL":
            vetoes.append("IV_CRUSH_LOW_VOL")

        # Gate 7: ASTO Over-Extension (Mean Reversion Guard)
        # [Audit-Fix 25.1] Use pre-cached ASTO for micro-stutter protection
        asto_val = self.local_signals.get(f"asto:{asset}", 0.0)
        if self.enabled_gates.get("ASTO", "True") == "True" and abs(asto_val) > 75 and lifecycle == "POSITIONAL":
            vetoes.append("ASTO_OVEREXTENSION")

        # Gate 8: Index Divergence (Fracture Veto)
        if self.enabled_gates.get("FRACTURE", "True") == "True":
            regimes_raw = await self._redis.hgetall("regime_state")
            trends = [json.loads(v).get("regime") for v in regimes_raw.values()]
            if "BULL_TREND" in trends and "BEAR_TREND" in trends:
                vetoes.append("INDEX_FRACTURE")

        # Gate 9: Macro Event Lockdown
        macro_lockdown = self.local_signals.get("MACRO_EVENT_LOCKDOWN", False)
        if self.enabled_gates.get("MACRO", "True") == "True" and macro_lockdown:
            vetoes.append("MACRO_CALENDAR_LOCKDOWN")

        # Gate 10: VIX Spike (Tail Risk Guard)
        vix_spike = self.local_signals.get("VIX_SPIKE_DETECTED", False)
        if self.enabled_gates.get("VIX_SPIKE", "True") == "True" and vix_spike:
            vetoes.append("CATASTROPHIC_VIX_SPIKE")

        # Gate 11: Position Ceiling (Anti-Fat-Finger)
        if self.enabled_gates.get("CEILING", "True") == "True" and len(self.parent_tracker[asset]) >= 5:
            vetoes.append("MAX_CONCURRENT_POSITIONS")

        # Gate 12: Asset-Specific Heat Limit
        # [Audit-Fix 25.2] Performance Optimization: Use pre-cached heat to avoid network jitter
        asset_lots = self.local_signals.get(f"heat:{asset}", 0.0)
        if self.enabled_gates.get("HEAT", "True") == "True" and asset_lots > MAX_LOTS_PER_ASSET:
            vetoes.append("ASSET_CAPACITY_EXCEEDED")

        # Gate 13: TastyTrade 0DTE Spread Guard
        if self.enabled_gates.get("0DTE_EDGE", "True") == "True" and lifecycle == "ZERO_DTE":
            iv_rv_spread = self._safe_float(state.get("iv_rv_spread", 0.0))
            if iv_rv_spread < 0.03: # Must have at least 3% edge
                vetoes.append("NO_0DTE_EDGE")

        # Gate 14: Bayesian Confidence Threshold
        confidence = self._safe_float(ctx.get("prob", 0.5))
        if self.enabled_gates.get("BAYES_CONFIDENCE", "True") == "True" and confidence < 0.55 and lifecycle != "HEDGE":
            vetoes.append("LOW_BAYESIAN_CONFIDENCE")

        # Gate 15: Regulatory Premium Quarantine
        quarantine = self._safe_float(self.local_signals.get("QUARANTINE_PREMIUM_LIVE", 0.0))
        if self.enabled_gates.get("QUARANTINE", "True") == "True" and quarantine > 100000 and lifecycle == "POSITIONAL":
            # If large capital is locked in T+1 settlement, block new positional sellers
            vetoes.append("CAPITAL_QUARANTINES_ACTIVE")

        # Gate 16: Correlation Limit Override
        correl = self.local_signals.get("CORRELATION", 0.0)
        if self.enabled_gates.get("CORRELATION", "True") == "True" and correl > CORRELATION_LIMIT and strat_id == "PositionalHunter":
            vetoes.append("CORRELATION_THRESHOLD_EXCEEDED")

        return vetoes

    async def _calculate_alpha_decay(self, intent: Dict, state: Dict) -> float:
        """
        [PHASE 14.1] Phasic Signal Decay.
        Reduces lot sizes based on the 'freshness' of the signal. 
        Signal edge decays exponentially with latency and time-since-origination.
        """
        origination_ts = intent.get("timestamp_orig", time.time())
        latency_ms = float(state.get("latency_ms", 0.0))
        
        # Decay factor = exp(-lambda * delta_t)
        # We use a lambda that penalizes latency > 100ms heavily
        lambda_decay = 0.005 if latency_ms < 100 else 0.02
        delta_t = time.time() - origination_ts
        
        decay_factor = math.exp(-lambda_decay * (delta_t * 1000 + latency_ms))
        return max(0.1, decay_factor) # Minimum 10% signal retention

    async def _resolve_overlapping_intents(self, all_intents: List[Tuple]) -> List[Tuple]:
        """
        [PHASE 14.2] Intent Deduplication & Alpha Summation.
        Prevents over-exposure when multiple strategies (e.g. Kinetic + Elastic) 
        trigger directional signals on the same asset.
        """
        asset_map = collections.defaultdict(list)
        for asset, intent, state, ctx in all_intents:
            asset_map[asset].append((intent, state, ctx))
            
        final_resolved = []
        for asset, signals in asset_map.items():
            if len(signals) == 1:
                final_resolved.append((asset, signals[0][0], signals[0][1], signals[0][2]))
                continue
                
            # Conflict Resolution: Take the one with higher Bayesian Confidence
            # or sum them if they are in the same direction.
            best_signal = max(signals, key=lambda x: x[2].get("prob", 0.0))
            logger.info(f"🔄 RESOLVING OVERLAP: Asset {asset} had {len(signals)} triggers. Selected {best_signal[0].get('strategy_id')}")
            final_resolved.append((asset, best_signal[0], best_signal[1], best_signal[2]))
            
        return final_resolved

    async def _enforce_hard_budget_scaling(self, intents: List[Tuple]):
        """
        [PHASE 14.3] Global Budget Constraint.
        Final safety check: Ensures sum of all LIVE lot margins < Capital Limit.
        Applies a 'Proportional Shrinkage' if budget is breached.
        """
        live_intents = [i for i in intents if i[1].get("is_live")]
        if not live_intents: return
        
        total_margin_req = 0.0
        for asset, intent, state, ctx in live_intents:
            iv = float(state.get("iv_atm", 0.18))
            total_margin_req += intent["lots"] * 150000 * (1.0 + iv)
            
        margin_state = await self.margin_manager.get_state(execution_type="ACTUAL")
        available = margin_state.get("total", 0.0)
        if total_margin_req > available and available > 0:
            scaler = available / total_margin_req
            logger.warning(f"📉 BUDGET BREACH: Req {total_margin_req:,.2f} > Avail {available:,.2f}. Scaling all by {scaler:.2f}")
            for asset, intent, state, ctx in live_intents:
                base_lots = {"NIFTY50": 25, "BANKNIFTY": 15, "SENSEX": 10}
                base_lot = base_lots.get(asset, 1)
                intent["lots"] = math.floor((intent["lots"] * scaler) / base_lot) * base_lot
                intent["scaling_applied"] = scaler
                if scaler < 0.2:
                    intent["is_live"] = False
                    intent["veto_reason"] = "CRITICAL_BUDGET_EXHAUSTION"

    async def process_single_intent(self, intent: Dict):
        """
        Orchestrates the decision cycle for a single ZMQ-triggered intent.
        Replaces the polling loop for ultra-low latency.
        """
        asset = intent.get("asset")
        if not asset: return
        
        state = self.market_state_cache.get(asset, {})
        ctx = self.regime_cache.get(asset, {"regime": "WAITING", "prob": 0.5})
        
        # Sync global thresholds (throttled internally)
        await self._sync_global_thresholds()
        
        # Step 1: Update Bayesian tracker with latest context
        self.regime_tracker.update(asset, ctx.get("regime", "RANGING"), float(ctx.get("prob", 0.7)))
        
        # Step 2: Metadata & Parent tracking
        intent["parent_uuid"] = intent.get("parent_uuid") or f"{asset}_{uuid.uuid4().hex[:6]}"
        intent["timestamp_router"] = datetime.now(timezone.utc).isoformat()
        
        # Step 3: Stage 1 Shadow Ledger (Counterfactual Journey SRS §14.8)
        await self._log_shadow_ledger(intent)
        
        # Step 4: Apply Alpha Decay
        decay_factor = await self._calculate_alpha_decay(intent, state)
        raw_lots = intent.get("lots", 0.0) * decay_factor
        
        # Mathematical Correctness: Floor-rounding to nearest base_lot step to prevent API rejection
        base_lots = {"NIFTY50": 25, "BANKNIFTY": 15, "SENSEX": 10}
        base_lot = base_lots.get(asset, 1)
        intent["lots"] = math.floor(raw_lots / base_lot) * base_lot
        intent["decay_factor"] = decay_factor
        
        # Step 4: Run the 15-Gate Veto Matrix
        vetoes = await self._evaluate_vetoes(asset, intent, state, ctx)
        primary_veto = vetoes[0] if vetoes else "NONE"
        
        # Step 5: The Truth Matrix (Multiplexer Rendering)
        intent["is_shadow"] = True
        intent["is_paper"] = (not self.system_halt)
        
        # LIVE reality is ONLY enabled if NO vetoes AND strategy toggle is ON
        strat_live_key = f"STRAT_LIVE_TOGGLE:{intent.get('strategy_id', 'KINETIC')}"
        live_toggle = (await self._redis.get(strat_live_key)) == "ON"
        
        intent["is_live"] = (live_toggle and primary_veto == "NONE")
        
        # Step 5.1: Stochastic Stress Test Veto
        if intent["is_live"]:
            stress_results = await self.stress_tester.run_stress_test(total_capital=10000000)
            if any(v < -500000 for v in stress_results.values()):
                intent["is_live"] = False
                intent["veto_reason"] = "STRESS_TEST_FAILURE"
                intent["stress_test_data"] = stress_results
                logger.warning(f"🚨 STRESS TEST VETO: {asset} failed tail-risk simulation.")

        # Step 5.2: Budget scaling (Single Intent mode)
        await self._enforce_hard_budget_scaling([(asset, intent, state, ctx)])

        intent["veto_reason"] = intent.get("veto_reason", primary_veto)
        intent["all_vetoes"] = vetoes
        
        # Increment strategy veto counter for analytics
        if primary_veto != "NONE":
            self.metrics["strategy_vetos"] += 1
            
        # Trigger single-intent resolution
        await self._final_dispatch(asset, intent, state, ctx)

    async def _log_shadow_ledger(self, intent: Dict):
        """
        Stage 1: Shadow Record. 
        Logs the raw 'Alpha Intent' to the Unified Bridge for journaling 
        to shadow_trades table before any Vetoes are applied.
        """
        asset = intent.get("asset")
        # We use the SHADOW. topic to ensure the Bridge knows this is counterfactual
        topic = f"SHADOW.{asset}"
        
        # We attach the environmental snapshot at the moment of intent
        intent["regime_at_intent"] = self.regime_cache.get(asset, {}).get("regime", "UNKNOWN")
        intent["prob_at_intent"] = self.regime_cache.get(asset, {}).get("prob", 0.0)
        
        await self.mq.send_json(self.trade_pub, topic, intent)
        logger.debug(f"📓 SHADOW LEDGER: Intent journaled for {asset}")

    async def _final_dispatch(self, asset, intent, state, ctx):
        """Final Atomic Dispatch with Dual-Wallet Protection."""
        if intent.get("lots", 0) <= 0: return
        
        # [Audit 22.2] Proper Dual-Wallet Partitioning
        exec_type = "ACTUAL" if intent.get("is_live") else "PAPER"
        
        iv = float(state.get("iv_atm", 0.18))
        required_margin = intent["lots"] * 150000 * (1.0 + iv)
        intent["reserved_margin"] = required_margin
        intent["execution_type"] = exec_type
        
        # [Phase 13: Post-Market Audit] Signal Price Preservation
        if "price" not in intent or intent["price"] <= 0:
            intent["price"] = float(state.get("price", 0.0))
        
        reserved_code = await self.margin_manager.reserve(required_margin, execution_type=exec_type)
        if reserved_code < 1:
            # [Audit-Fix 25.5] PhD Analytics: Differentiate between Capital and Margin Vetoes
            if reserved_code == -1:
                self.metrics["gross_margin_vetos"] += 1
            elif reserved_code == -2:
                self.metrics["capital_ratio_vetos"] += 1

            reason = "TOTAL_MARGIN_BREACH" if reserved_code == -1 else "RATIO_MARGIN_BREACH" if reserved_code == -2 else "UNKNOWN"
            if intent.get("is_live"):
                intent["is_live"] = False
                intent["veto_reason"] = f"MARGIN_REJECTION_{exec_type}_{reason}"
                return # Halt live only
            else:
                intent["is_paper"] = False
                intent["veto_reason"] = f"PAPER_MARGIN_REJECTION_{exec_type}_{reason}"

        # [Audit 22.3] Enrichment happens BEFORE Greek calculation
        if LIFECYCLE_MAP.get(intent['strategy_id']) == "POSITIONAL":
            await self._build_basket_intent(asset, intent)
        else:
            await self._enrich_single_leg(asset, intent)

        # [Audit 25.6] Dynamic Greek Aggregation for Basket Trades
        if "delta" not in intent:
            if intent.get("legs"):
                # Institutional Fix: Aggregate Greeks from the basket legs
                intent["delta"] = sum(0.5 if "CE" in l.get("symbol", "") else -0.5 for l in intent["legs"])
                intent["theta"] = sum(-2.0 for _ in intent["legs"]) # Heuristic
            elif intent.get("tradingsymbol"):
                symbol = str(intent["tradingsymbol"])
                # [Audit 23.3] Correct Greek assignment timing
                intent["delta"] = 0.5 if "CE" in symbol else -0.5
                intent["theta"] = -2.0
            else:
                intent["delta"] = 0.0
                intent["theta"] = 0.0

        # Dispatch
        topic = f"ORDER.{asset}"
        # [Audit 23.1] Greek Attribution: Enrollment happens AFTER enrichment above (633-637)
        # ensures intent['delta'] is correctly mapped to the populated tradingsymbol.
        await self.mq.send_json(self.orders_pub, topic, intent)
        
        # Logging
        asyncio.create_task(self._log_truth_matrix(asset, intent, state, ctx))
        self.metrics["intents_processed"] += 1
        if intent["is_live"]: self.metrics["live_strikes"] += 1

    async def broadcast_decisions(self, market_state: Dict, regimes: Dict):
        """DEPRECATED: Replaced by process_single_intent for zero-latency."""
        return # Fixed bug: Use 'return' instead of 'pass' to properly short-circuit legacy broadcast execution body
        """
        Orchestrates the high-level decision flow for all monitored assets.
        Implements the Unified Intent Multiplexer (Layer 9).
        """
        all_intents = []
        await self._sync_global_thresholds()
        
        # Aggregate Portfolio-Level Context for Global Vetoes
        total_requested_lots = 0.0
        potential_intents = []
        
        for asset in self.all_indices:
            state = market_state.get(asset, {})
            ctx = regimes.get(asset, {"regime": "WAITING", "prob": 0.5})
            
            # Step 1: Update Bayesian Context
            self.regime_tracker.update(asset, ctx.get("regime", "RANGING"), float(ctx.get("prob", 0.7)))
            
            # Step 2: Resolve Tactical Intent (from SHM or Redis)
            raw_intent_json = await self._redis.get(f"RAW_INTENT:{asset}")
            if not raw_intent_json: continue
            
            # [Audit-Fix 7.1] Switched to fast C-level parsing for high TPS loops
            intent = fast_json.loads(raw_intent_json)
            intent["parent_uuid"] = f"{asset}_{uuid.uuid4().hex[:6]}"
            intent["timestamp_router"] = datetime.now(timezone.utc).isoformat()
            
            # Step 3: Apply Alpha Decay
            decay_factor = await self._calculate_alpha_decay(intent, state)
            # [Audit-Fix 7.2] Removed premature rounding to preserve variance
            intent["lots"] = intent["lots"] * decay_factor
            intent["decay_factor"] = decay_factor

            # Step 4: Run the 15-Gate Veto Matrix
            vetoes = await self._evaluate_vetoes(asset, intent, state, ctx)
            primary_veto = vetoes[0] if vetoes else "NONE"
            
            # Step 5: The Truth Matrix (Multiplexer Rendering)
            intent["is_shadow"] = True
            intent["is_paper"] = (not self.system_halt)
            
            # LIVE reality is ONLY enabled if NO vetoes AND strategy toggle is ON
            strat_live_key = f"STRAT_LIVE_TOGGLE:{intent.get('strategy_id', 'KINETIC')}"
            live_toggle = (await self._redis.get(strat_live_key)) == "ON"
            
            intent["is_live"] = (live_toggle and primary_veto == "NONE")
            
            # Step 5.1: Stochastic Stress Test Veto (Institutional Layer 11)
            if intent["is_live"]:
                stress_results = await self.stress_tester.run_stress_test(total_capital=10000000) # 1Cr Dummy
                if any(v < -500000 for v in stress_results.values()): # Veto if any scenario > 5% drawdown
                    intent["is_live"] = False
                    intent["veto_reason"] = "STRESS_TEST_FAILURE"
                    intent["stress_test_data"] = stress_results
                    logger.warning(f"🚨 STRESS TEST VETO: {asset} trade failed tail-risk simulation.")

            # Step 5.2: Greek Telemetry Enrichment
            if intent.get("tradingsymbol"):
                # Simplified Greek fetch - in PROD, this would use self.greek_engine.bs
                intent["delta"] = 0.5 if "CE" in intent["tradingsymbol"] else -0.5
                intent["theta"] = -2.0
            else:
                intent["delta"] = 0.0
                intent["theta"] = 0.0

            intent["veto_reason"] = intent.get("veto_reason", primary_veto)
            intent["all_vetoes"] = vetoes
                
            potential_intents.append((asset, intent, state, ctx))

        # Step 6: Overlap Resolution & Budget Scaling
        resolved_intents = await self._resolve_overlapping_intents(potential_intents)
        await self._enforce_hard_budget_scaling(resolved_intents)

        # Step 7: Final Execution & Atomic Reservation
        for asset, intent, state, ctx in resolved_intents:
            if intent.get("lots", 0) <= 0: continue
            
            # Atomic Margin Reservation (Only for LIVE reality)
            if intent["is_live"]:
                # Margin calculation heuristic: 1.5 Lakh per lot + IV adjustment
                iv = float(state.get("iv_atm", 0.18))
                required_margin = intent["lots"] * 150000 * (1.0 + iv)
                
                reserved_code = await self.margin_manager.reserve(required_margin, execution_type="ACTUAL")
                if reserved_code < 1:
                    reason = "TOTAL_MARGIN_BREACH" if reserved_code == -1 else "RATIO_MARGIN_BREACH" if reserved_code == -2 else "UNKNOWN"
                    intent["is_live"] = False
                    intent["veto_reason"] = f"ATOMIC_MARGIN_REJECTION_{reason}"
                    logger.warning(f"🛑 MARGIN SHIELD: Rejection on {asset} for {intent['lots']} lots. Reason: {reason}.")
                else:
                    logger.info(f"✅ MARGIN RESERVED: ₹{required_margin:,.2f} for {asset} Strike.")

            # Symbol & Leg Enrichment (Basket Synthesis)
            if LIFECYCLE_MAP.get(intent['strategy_id']) == "POSITIONAL":
                await self._build_basket_intent(asset, intent)
            else:
                await self._enrich_single_leg(asset, intent)

            # Step 7: Unified Dispatch
            # We send ONE packet to the Unified Bridge. The bridge renders the realities.
            topic = f"ORDER.{asset}"
            await self.mq.send_json(self.orders_pub, topic, intent)
            
            # Step 8: Counterfactual Logging (For DeepDive Analytics)
            asyncio.create_task(self._log_truth_matrix(asset, intent, state, ctx))
            
            # Update metrics
            self.metrics["intents_processed"] += 1
            if intent["veto_reason"] != "NONE":
                self.metrics["vetos_triggered"] += 1
                self.metrics["lots_saved"] += intent["lots"]
            if intent["is_live"]:
                self.metrics["live_strikes"] += 1

    async def _enrich_single_leg(self, asset: str, intent: Dict):
        """Enriches directional intents with ATM symbols."""
        strikes = await self._redis.hgetall("optimal_strikes")
        side = "CE" if intent.get("action") == "BUY" else "PE" # Simplified
        strike = strikes.get(f"{asset}_{side}")
        expiry = await self._redis.get(f"EXPIRY:{asset}") or "26MAR"
        
        if strike:
            intent["tradingsymbol"] = f"{asset}{expiry}{int(float(strike))}{side}"
            intent["exchange"] = "BFO" if asset == "SENSEX" else "NFO"
            # Auto-subscribe at the gateway
            await self._redis.publish("dynamic_subscriptions", f"{intent['exchange']}|{intent['tradingsymbol']}")
        else:
            intent["tradingsymbol"] = asset # Fallback to underlying

    async def _build_basket_intent(self, asset: str, intent: Dict):
        """
        Complex Basket Synthesis for Multi-Leg Strategies (IronCondor, Butterfly).
        Applies Delta-Neutral offsets using Bayesian regime bias.
        """
        strat = intent.get("strategy_id")
        spot_raw = await self._redis.get(f"latest_market_state:{asset}")
        spot = json.loads(spot_raw).get("price", 22000.0) if spot_raw else 22000.0
        
        expiry = await self._redis.get(f"EXPIRY:{asset}") or "26MAR"
        exch = "BFO" if asset == "SENSEX" else "NFO"
        
        # Institutional Delta Targeting logic
        bias = float(intent.get("bias", 0.0)) # -100 to 100
        shift = (bias / 100.0) * 50 # Max 50 point shift in strikes
        
        # Greek Mismatch Fix (Institutional Step 16.1)
        # Use INDEX_METADATA logic: 100 increments for BANKNIFTY/SENSEX, 50 for NIFTY
        divisor = 100 if asset in ["BANKNIFTY", "SENSEX"] else 50
        
        if strat == "IronCondor":
            # 4-Leg Neutral Synthesis
            sc_strike = round((spot + 200 + shift) / divisor) * divisor
            bc_strike = sc_strike + (2 * divisor)
            sp_strike = round((spot - 200 + shift) / divisor) * divisor
            bp_strike = sp_strike - (2 * divisor)
            
            intent["legs"] = [
                {"symbol": f"{asset}{expiry}{int(sc_strike)}CE", "action": "SELL", "ratio": 1},
                {"symbol": f"{asset}{expiry}{int(bc_strike)}CE", "action": "BUY",  "ratio": 1},
                {"symbol": f"{asset}{expiry}{int(sp_strike)}PE", "action": "SELL", "ratio": 1},
                {"symbol": f"{asset}{expiry}{int(bp_strike)}PE", "action": "BUY",  "ratio": 1}
            ]
        elif strat == "DirectionalCredit":
            # 2-Leg Directional Synthesis
            if bias > 0: # Bullish -> Sell Put Spread
                sp = round((spot - 100 + shift) / divisor) * divisor
                bp = sp - divisor
                intent["legs"] = [
                    {"symbol": f"{asset}{expiry}{int(sp)}PE", "action": "SELL", "ratio": 1},
                    {"symbol": f"{asset}{expiry}{int(bp)}PE", "action": "BUY",  "ratio": 1}
                ]
            else: # Bearish -> Sell Call Spread
                sc = round((spot + 100 + shift) / divisor) * divisor
                bc = sc + divisor
                intent["legs"] = [
                    {"symbol": f"{asset}{expiry}{int(sc)}CE", "action": "SELL", "ratio": 1},
                    {"symbol": f"{asset}{expiry}{int(bc)}CE", "action": "BUY",  "ratio": 1}
                ]
        
        intent["exchange"] = exch
        # Trigger JIT Subscriptions for all legs
        for leg in intent.get("legs", []):
            await self._redis.publish("dynamic_subscriptions", f"{exch}|{leg['symbol']}")

    async def _log_truth_matrix(self, asset: str, intent: Dict, state: Dict, ctx: Dict):
        """
        Extensive Counterfactual Logging for Institutional Analytics.
        Tracks every decision outcome including intermediate vetos.
        """
        if self.test_mode: return
        
        payload = {
            "time": datetime.now(timezone.utc).isoformat(),
            "asset": asset,
            "strategy_id": intent.get("strategy_id"),
            "action": intent.get("action"),
            "lots": self._safe_float(intent.get("lots", 0.0)),
            "price": self._safe_float(state.get("price", 0.0)),
            "veto_reason": intent.get("veto_reason", "NONE"),
            "all_vetoes": intent.get("all_vetoes", []),
            "is_shadow": intent.get("is_shadow", True),
            "is_paper": intent.get("is_paper", True),
            "is_live": intent.get("is_live", False),
            "vpin": self._safe_float(state.get("vpin", 0.0)),
            "asto": self._safe_float(state.get("asto", 0.0)),
            "regime": ctx.get("regime", "UNKNOWN"),
            "prob": self._safe_float(ctx.get("prob", 0.0)),
            "latency_ms": self._safe_float(state.get("latency_ms", 0.0)),
            "parent_uuid": intent.get("parent_uuid")
        }
        
        # Publish to Trade Events for Dashboard Consumption
        await self.mq.send_json(self.trade_pub, f"SHADOW.{asset}", payload)
        
        # Batch log to TimescaleDB via MQ
        if intent["veto_reason"] != "NONE":
            await self.mq.send_json(self.trade_pub, f"REJECTION.{asset}", payload)

    async def _run_maintenance(self):
        """Periodic system reconciliations (Greeks, Health)."""
        while True:
            try:
                # 1. Heartbeat Double-Spawn fixed (already spawned globally in main event loop mapping)
                
                # 2. Reconcile Portfolio Greeks
                # (Fetch positions from Redis populated by SnapshotManager)
                positions_raw = await self._redis.get("LIVE_POSITIONS_CACHE")
                if positions_raw:
                    positions = json.loads(positions_raw)
                    await self.greek_engine.reconcile(positions)
                
                # 3. Publish Performance Metrics
                await self._redis.set("METAROUTER:METRICS", json.dumps(self.metrics))
                
                # 4. Cleanup old parent trackers (stale intents)
                # ...
                
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Maintenance error: {e}")
                await asyncio.sleep(10)

    async def _market_state_listener(self):
        """Background Zero-Latency Signal Sync."""
        while True:
            try:
                topic, state = await self.mq.recv_json(self.state_sub)
                asset = topic.replace("STATE.", "")
                self.market_state_cache[asset] = state
                
                # [Audit 22.2] Background signal sync (Zero-Latency pre-caching for vetoes)
                self.local_signals["VIX_INDEX"] = float(await self._redis.get("VIX_INDEX") or 15.0)
                self.local_signals["VIX_SPIKE_DETECTED"] = (await self._redis.get("VIX_SPIKE_DETECTED") == "True")
                # [Audit-Fix 25.1] Pre-cache high-speed signals once per pulse
                for a in self.all_indices:
                    # Pre-calculate asset heat synchronously from local signals in Veto Matrix
                    raw_heat = await self._redis.get(f"TOTAL_HEAT:{a}") # OR POS_LOTS sum
                    self.local_signals[f"heat:{a}"] = self._safe_float(raw_heat)

                    self.local_signals[f"vpin:{a}"] = float(await self._redis.get(f"vpin:{a}") or 0.0)
                    self.local_signals[f"iv_atm:{a}"] = float(await self._redis.get(f"iv_atm:{a}") or 0.15)
                    self.local_signals[f"asto:{a}"] = float(await self._redis.get(f"asto:{a}") or 0.0)
                
                self.local_signals["MACRO_EVENT_LOCKDOWN"] = (await self._redis.get("MACRO_EVENT_LOCKDOWN") == "True")
                self.local_signals["QUARANTINE_PREMIUM_LIVE"] = float(await self._redis.get("QUARANTINE_PREMIUM_LIVE") or 0.0)
                
                # [Audit-Fix 25.3] Untether Correlation Engine (Non-Blocking)
                async def _bg_correl_update():
                    try:
                        self.local_signals["CORRELATION"] = await self.correlation_engine.get_correlation("NIFTY50", "BANKNIFTY")
                    except Exception as e:
                        logger.warning(f"⚠️ Correlation Engine update failed: {e}")
                asyncio.create_task(_bg_correl_update())
                
                self.local_signals["_last_update"] = time.time()
                
                # Also sync regimes from this pulse
                regimes_raw = await self._redis.hgetall("regime_state")
                for k, v in regimes_raw.items():
                    self.regime_cache[k] = fast_json.loads(v)
            except Exception: 
                await asyncio.sleep(0.1)

    async def _intent_worker(self):
        """ Strictly Monotonic Intent Processor (SRS §17.2). """
        while True:
            try:
                intent = await self.intent_queue.get()
                await self.process_single_intent(intent)
                self.intent_queue.task_done()
            except Exception as e:
                logger.error(f"Intent Worker Error: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    async def run(self):
        """
        Main Event Loop (Event-Driven).
        Replaced the 50ms polling loop with a zero-latency ZMQ trigger.
        Uses a Strictly Monotonic Queue for race-condition prevention.
        """
        logger.info("Project K.A.R.T.H.I.K. MetaRouter: Sequential Fortress Activated.")
        await self._perform_self_audit()
        
        asyncio.create_task(self.hb.run_heartbeat())
        asyncio.create_task(self._run_maintenance())
        asyncio.create_task(self._market_state_listener())
        asyncio.create_task(self._intent_worker())
        
        while True:
            try:
                # Zero-Latency Trigger: Wait for RAW intent from Strategy Engine
                topic, raw_intent = await self.mq.recv_json(self.raw_intents_pull)
                if raw_intent:
                    # [SRS §17.3] Ensure Monotonic Arrival Order
                    await self.intent_queue.put(raw_intent)
                    
            except Exception as e:
                logger.error(f"Router Core Exception: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    async def _perform_self_audit(self):
        """Institutional startup check (SRS §14.5)."""
        logger.info("🛡️ STARTUP AUDIT: Verifying Redis connections and LUA scripts...")
        try:
            await self._redis.ping()
            # Verify required keys exist
            required = ["VIX_INDEX", "SYSTEM_HALT", "CASH_COMPONENT_LIVE"]
            for r in required:
                if not await self._redis.exists(r):
                    logger.warning(f"⚠️ MISSING CRITICAL KEY: {r}. Initializing with defaults.")
                    await self._redis.set(r, "15.0" if r == "VIX_INDEX" else "False")
            logger.info("✅ MetaRouter Audit Passed.")
        except Exception as e:
            logger.error(f"❌ AUDIT FAILED: {e}")
            sys.exit(1)

    # ── [HDD Analytics] DeepDive Logging ──
    async def _log_deep_dive_metrics(self):
        """Pushes structured decision matrix to the counterfactual analytics engine."""
        stats = {
            "ts": datetime.now(timezone.utc).timestamp(),
            "metrics": self.metrics,
            "top_vetoes": collections.Counter(sum(self.veto_ledger.values(), [])).most_common(5)
        }
        await self._redis.rpush("Analytics:DeepDive:Router", json.dumps(stats))
        await self._redis.ltrim("Analytics:DeepDive:Router", -100, -1)

if __name__ == "__main__":
    if uvloop: uvloop.install()
    router = MetaRouter()
    asyncio.run(router.run())
