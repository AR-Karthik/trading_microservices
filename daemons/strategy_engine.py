import asyncio
import logging
import uuid
import zmq
import numpy as np # type: ignore
import collections
import json
import random
import time
import sys
import pandas as pd
import os
from redis import asyncio as redis
import math
from datetime import datetime, timezone
from core.logger import setup_logger
from core.mq import MQManager, Ports, RedisLogger, Topics, NumpyEncoder  # [R2-01] Added NumpyEncoder
from core.greeks import BlackScholes
import re
from core.shared_memory import TickSharedMemory, SYMBOL_TO_SLOT
from core.shm import ShmManager, SignalVector, RegimeShm, RegimeVector
from core.margin import AsyncMarginManager

try:
    import uvloop
except ImportError:
    uvloop = None

logger = setup_logger("StrategyEngine", log_file="logs/strategy_engine.log")
redis_logger = RedisLogger()

# NumpyEncoder now imported from core.mq (Audit 4.1)

# --- Recommendation 7: Shadow Trading Flag ---
IS_SHADOW_MODE = "--shadow" in sys.argv

# --- [Audit-Fix] Index Metadata for Multi-Index Robustness ---
INDEX_METADATA = {
    "NIFTY50": {
        "increment": 50,
        "lot_size": 50,
        "expiry_day": "Thursday",
        "underlying": "NIFTY"
    },
    "BANKNIFTY": {
        "increment": 100,
        "lot_size": 15,  # [Audit-Fix] Updated BN lot size to 15
        "expiry_day": "Wednesday",
        "underlying": "BANKNIFTY"
    },
    "SENSEX": {
        "increment": 100,
        "lot_size": 10,
        "expiry_day": "Friday",
        "underlying": "SENSEX"
    }
}

class BaseStrategy:
    def __init__(self, strategy_id: str, symbols: list[str], **kwargs):
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.positions: collections.defaultdict[str, int] = collections.defaultdict(int)
        # Using a fixed size deque for history to avoid memory growth
        self.history: collections.defaultdict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=100))
        self.schedule: dict = {}
        self.execution_type = "Paper" # Default to paper trading
        self.is_shadow = False # Default to not shadow trading

    def on_tick(self, symbol: str, data: dict) -> str | list | None:
        """Standard interface for all strategies. data contains price, oi, etc."""
        raise NotImplementedError

    def check_exit(self, symbol: str, data: dict) -> str | list | None:
        """[Audit-Fix] Alpha-Death Exit: Exits based on math failure, not price stops."""
        return None

    def is_active_now(self):
        """Checks if the strategy should be active based on its schedule."""
        if not hasattr(self, 'schedule') or not self.schedule:
            return True
            
        now = datetime.now()
        current_day = now.strftime("%A")
        current_time = now.strftime("%H:%M")
        
        days = self.schedule.get('days', [])
        
        if days and current_day not in days:
            return False
            
        slots = self.schedule.get('slots', [])
        # Backwards compatibility for single-slot configs
        if not slots and 'start' in self.schedule and 'end' in self.schedule:
            slots = [{'start': self.schedule['start'], 'end': self.schedule['end']}]
            
        if not slots:
            return True
            
        for slot in slots:
            if slot.get('start', "00:00") <= current_time <= slot.get('end', "23:59"):
                return True
                
        return False



class DirectionalCreditSpreadStrategy(BaseStrategy):
    """
    POSITIONAL: Professional Credit Spread strategy (2-leg).
    Sells OTM options and buys further OTM for margin efficiency and tail protection.
    """
    def __init__(self, strategy_id: str, symbols: list[str], spread_width: float = 100, target_delta: float = 0.25, **kwargs):  # [R2-12] 0.20→0.25 per spec
        super().__init__(strategy_id, symbols)
        self.spread_width = float(spread_width)
        self.target_delta = float(target_delta)
        self.bs = BlackScholes()

    def on_tick(self, symbol: str, data: dict) -> list | None:
        # Logistic governed by Meta-Router/Regime. Sensor provides alpha.
        # This strategy is triggered for 'POSITIONAL' entries.
        # [Audit-Fix] Double-Key Authorization: Target S18 = 1 (Trending)
        s18 = data.get("regime_s18", 0)
        if s18 != 1: return None
        
        # [Audit-Fix] Use 1.5x ATR or Delta for robust strike selection
        atr = data.get("atr", 100.0)
        offset = max(increment * 2, atr * 1.5)

        # Directional logic: alpha balance determines CE vs PE
        alpha = data.get('s_total', 0)
        if alpha > 25: # Bullish Bias -> Bull Put Spread
            s1 = round((price - offset) / increment) * increment
            s2 = s1 - self.spread_width
            parent_uuid = f"DCS_{uuid.uuid4().hex[:8]}"
            return [
                {"action": "SELL", "otype": "PE", "strike": s1, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol},
                {"action": "BUY",  "otype": "PE", "strike": s2, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol}
            ]
        elif alpha < -25: # Bearish Bias -> Bear Call Spread
            s1 = round((price + offset) / increment) * increment
            s2 = s1 + self.spread_width
            parent_uuid = f"DCS_{uuid.uuid4().hex[:8]}"
            return [
                {"action": "SELL", "otype": "CE", "strike": s1, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol},
                {"action": "BUY",  "otype": "CE", "strike": s2, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol}
            ]
        return None

class TastyTrade0DTEStrategy(BaseStrategy):
    """
    ZERO_DTE: High-frequency theta harvesting for 0-DTE.
    Enters ATM strangles/straddles to capture rapid decay with strict kinetic stops.
    """
    def __init__(self, strategy_id: str, symbols: list[str], **kwargs):
        super().__init__(strategy_id, symbols)

    def on_tick(self, symbol: str, data: dict) -> list | None:
        now = datetime.now()
        # [SE-02] 0-DTE Schedule: BANKNIFTY (Wed), NIFTY50 (Thu), SENSEX (Fri)
        expiry_days = {
            "BANKNIFTY": "Wednesday",
            "NIFTY50": "Thursday",
            "SENSEX": "Friday"
        }
        
        current_day = now.strftime("%A")
        if symbol in INDEX_METADATA and current_day != INDEX_METADATA[symbol]["expiry_day"]:
            return None
            
        # [S-01] Active 0-DTE window: 09:30 to 10:30 IST
        current_time = now.hour * 60 + now.minute  # minutes since midnight
        if current_time < 570 or current_time > 630:  # 09:30=570, 10:30=630
            return None
        
        price = float(data['price'])
        # [SE-01] Generalize strike rounding based on asset
        meta = INDEX_METADATA.get(symbol, INDEX_METADATA["NIFTY50"])
        increment = meta["increment"]
        
        # [S-02] Iron Butterfly (protected straddle) instead of naked straddle
        # [S-03] Tasty Edge Check: IV-RV Spread must be > 3.0%
        iv_rv_spread = data.get("iv_rv_spread", 0.0)
        if iv_rv_spread < 0.03:
            return None # No edge, no trade.

        atm_strike = round(price / increment) * increment
        wing_offset = increment * 4  # e.g., 200 for NIFTY, 400 for others
        parent_uuid = f"TT0_{uuid.uuid4().hex[:8]}"
        return [
            {"action": "SELL", "otype": "CE", "strike": atm_strike, "parent_uuid": parent_uuid, "lifecycle": "ZERO_DTE", "underlying": symbol},
            {"action": "BUY",  "otype": "CE", "strike": atm_strike + wing_offset, "parent_uuid": parent_uuid, "lifecycle": "ZERO_DTE", "underlying": symbol},
            {"action": "SELL", "otype": "PE", "strike": atm_strike, "parent_uuid": parent_uuid, "lifecycle": "ZERO_DTE", "underlying": symbol},
            {"action": "BUY",  "otype": "PE", "strike": atm_strike - wing_offset, "parent_uuid": parent_uuid, "lifecycle": "ZERO_DTE", "underlying": symbol}
        ]

class ElasticHunterStrategy(BaseStrategy):
    """
    S02: Mean Reversion Hunter. 
    Target: S18 = 0 (Neutral) or 2 (Ranging).
    Trigger: Price > 2.0 Sigma from VWAP + RSI Divergence.
    """
    def on_tick(self, symbol: str, data: dict) -> list | None:
        s18 = data.get("regime_s18", 0)
        if s18 not in [0, 2]: return None # Veto if Trending or Volatile

        # [Audit-Fix] Threshold at 2.0 per spec
        z_score = data.get("price_zscore", 0.0)
        if abs(z_score) > 2.0:
            parent_uuid = f"ELAS_{uuid.uuid4().hex[:8]}"
            # Bet on return to Mean (VWAP)
            action = "SELL" if z_score > 2.0 else "BUY"
            return [{"action": action, "symbol": symbol, "parent_uuid": parent_uuid, "lifecycle": "ELASTIC"}]
        return None

    def check_exit(self, symbol: str, data: dict) -> list | None:
        """Elastic Exit: Kill trade if price touches VWAP (Mean Reversion complete)."""
        if self.positions.get(symbol, 0) == 0: return None
        price = data.get("price", 0.0)
        vwap = data.get("vwap", price)
        # Check for crossover
        pos = self.positions[symbol]
        if (pos > 0 and price >= vwap) or (pos < 0 and price <= vwap):
            logger.info(f"🎯 ELASTIC EXIT: Price reached Mean (VWAP). Closing {self.strategy_id} for {symbol}.")
            return [{"action": "SELL" if pos > 0 else "BUY", "symbol": symbol, "lifecycle": "ELASTIC", "reason": "VWAP_TOUCH"}]
        return None

class KineticHunterStrategy(BaseStrategy):
    """
    S01: Trend Movement Specialist. 
    Target: S18 = 1 (Trending).
    Trigger: |ASTO| > 70 + Smart Flow (S25) > 20 + Whale Alignment (S22).
    """
    def on_tick(self, symbol: str, data: dict) -> list | None:
        s18 = data.get("regime_s18", 0)
        if s18 != 1: return None # Veto if NOT Trending
        
        # [Audit-Fix] Kinetic Triggers (S22 + S23 + S25)
        asto = data.get("asto", 0.0)
        smart_flow = data.get("smart_flow", 0.0)
        whale_p = data.get("whale_pivot", 0.0)
        
        # Trend check: |ASTO| > 70 and Smart Flow alignment
        if abs(asto) > 70 and abs(smart_flow) > 20:
            # [Audit-Fix] Power Five Pulse Gate (Lead-Lag Confirmation)
            # Verify 3/5 top heavyweights are aligned with the index move
            hw_alphas = data.get("hw_alphas", [])
            if len(hw_alphas) >= 5:
                # RELIANCE, HDFCBANK, ICICIBANK, INFY, TCS are indices 0-4
                power_five = hw_alphas[:5]
                bullish_count = sum(1 for a in power_five if a > 10) # 10 as minimum alpha pulse
                bearish_count = sum(1 for a in power_five if a < -10)
                
                if asto > 70 and bullish_count < 3:
                    return None # Fails lead-lag confirmation
                if asto < -70 and bearish_count < 3:
                    return None
            
            # Direction check: All 3 signals must align
            is_bullish = (asto > 70 and smart_flow > 20 and whale_p > 0)
            is_bearish = (asto < -70 and smart_flow < -20 and whale_p < 0)
            
            if is_bullish or is_bearish:
                parent_uuid = f"KINE_{uuid.uuid4().hex[:8]}"
                action = "BUY" if is_bullish else "SELL"
                return [{"action": action, "symbol": symbol, "parent_uuid": parent_uuid, "lifecycle": "KINETIC"}]
        return None

    def check_exit(self, symbol: str, data: dict) -> list | None:
        """Kinetic Exit: Kill trade if Whale Pivot (S22) flips (Alpha Death)."""
        if self.positions.get(symbol, 0) == 0: return None
        pos = self.positions[symbol]
        whale_p = data.get("whale_pivot", 0.0)
        if (pos > 0 and whale_p < 0) or (pos < 0 and whale_p > 0):
            logger.info(f"🏹 KINETIC EXIT: Whale Pivot (S22) flipped. Momentum dead for {symbol}.")
            return [{"action": "SELL" if pos > 0 else "BUY", "symbol": symbol, "lifecycle": "KINETIC", "reason": "S22_FLIP"}]
        return None

class PositionalHunterStrategy(BaseStrategy):
    """
    S11: Theta/Edge Harvesting Specialist.
    Target: S18 = 2 (Ranging).
    Trigger: IV-RV Spread > 3% + Persistence (S26) > 60%.
    """
    def on_tick(self, symbol: str, data: dict) -> list | None:
        s18 = data.get("regime_s18", 0)
        if s18 != 2: return None # Veto if NOT Ranging
        
        # [Audit-Fix] Positional Triggers (S12 + S26)
        iv_rv = data.get("iv_rv_spread", 0.0)
        s26 = data.get("persistence_s26", 0.0)
        
        if iv_rv > 0.03 and s26 > 60:
            # Mean Reversion entry logic for credit spreads
            z_score = data.get("price_zscore", 0.0)
            side = "bull" if z_score < 0 else "bear"
            
            # Delegates to internal Credit Spread logic
            parent_uuid = f"POSI_{uuid.uuid4().hex[:8]}"
            # We use 100 wide wings for conservative yield
            spread_width = 100
            price = data.get("price", 0.0)
            increment = 50 if "NIFTY" in symbol else 100
            
            if side == "bull":
                s1 = round((price - 100) / increment) * increment
                s2 = s1 - spread_width
                return [
                    {"action": "SELL", "otype": "PE", "strike": s1, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol},
                    {"action": "BUY",  "otype": "PE", "strike": s2, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol}
                ]
            else:
                s1 = round((price + 100) / increment) * increment
                s2 = s1 + spread_width
                return [
                    {"action": "SELL", "otype": "CE", "strike": s1, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol},
                    {"action": "BUY",  "otype": "CE", "strike": s2, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol}
                ]
        return None


class GammaScalpingStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, symbols: list[str], strike: float = 22000, expiry_days: float = 30, iv: float = 0.15, r: float = 0.065, hedge_threshold: float = 0.10, **kwargs):
        super().__init__(strategy_id, symbols)
        self.strike = float(strike)
        self.expiry_years = float(expiry_days) / 365.0
        self.iv = float(iv)
        # [Audit 5.4, 9.4] Dynamic risk-free rate setup, fallback to RBI Repo (0.065)
        self.r = float(kwargs.get("risk_free_rate", r))
        self.hedge_threshold = float(hedge_threshold)
        self.last_calc_time = datetime.now(timezone.utc)

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = float(data['price'])
        now = datetime.now(timezone.utc)
        dt = (now - self.last_calc_time).total_seconds() / (365.0 * 24 * 3600)
        self.expiry_years -= dt
        self.last_calc_time = now
        
        if self.expiry_years <= 0:
            # [R2-16] Reset from Redis or default to prevent permanent disable
            self.expiry_years = 30.0 / 365.0
            return None
        
        # [Audit 1.3] Check if symbol is an option (e.g. contains CE or PE) to adjust strike
        dynamic_strike = self.strike
        if "CE" in symbol or "PE" in symbol:
            try:
                # Basic decoder, assuming NIFTY24APR22000CE format
                match = re.search(r'(\d+)(CE|PE)$', symbol)
                if match: dynamic_strike = float(match.group(1))
            except Exception: pass

        bs = BlackScholes()
        delta = bs.delta(price, dynamic_strike, self.expiry_years, self.r, self.iv, "call" if "CE" in symbol else "put")
        
        current_pos = float(self.positions[symbol])
        target_pos = -float(delta * 100) # Delta hedging assumes 100 lot for simple math
        
        error = target_pos - current_pos
        
        if abs(error) >= self.hedge_threshold:
            return "BUY" if error > 0 else "SELL"
        return None

class IronCondorStrategy(BaseStrategy):
    """Phase 7: Neutral market strategy using 4 option legs with dynamic strike selection."""
    def __init__(self, strategy_id: str, symbols: list[str], wing_delta: float = 0.10, spread_delta: float = 0.15, expiry_days: float = 7, iv: float = 0.15, **kwargs):  # [F9-03] spread_delta 0.25→0.15
        super().__init__(strategy_id, symbols)
        self.wing_delta = float(wing_delta)
        self.spread_delta = float(spread_delta)
        self.expiry_years = float(expiry_days) / 365.0
        self.iv = float(iv)
        self.bs = BlackScholes()
        # [Hedge Hybrid] Track leg metadata for delta calculation
        self.leg_metadata: dict[str, dict] = {}
        self.last_parent_uuid = "IC_INITIAL"

    def find_strike_for_delta(self, target_delta: float, S: float, T: float, r: float, sigma: float, otype: str) -> float:
        """Binary search to find strike for target delta."""
        low, high = S * 0.5, S * 1.5
        for _ in range(15): # 15 iterations for precision
            mid = (low + high) / 2
            d = self.bs.delta(S, mid, T, r, sigma, otype)
            if (otype == 'call' and d > target_delta) or (otype == 'put' and d < target_delta):
                low = mid if otype == 'call' else low
                high = mid if otype == 'put' else high
            else:
                high = mid if otype == 'call' else high
                low = mid if otype == 'put' else low
        
        # [SE-01] Generalize strike rounding based on asset (inferred from symbol)
        # Assuming first symbol in list is representative or self has context
        asset = self.symbols[0] if self.symbols else "NIFTY50"
        increment = 100 if "BANKNIFTY" in asset or "SENSEX" in asset else 50
        return round(mid / increment) * increment # Snap to asset-specific strike

    def _calculate_net_delta(self, spot: float, r: float) -> float:
        """Calculates total delta across all active option legs."""
        net_delta = 0.0
        for opt_symbol, qty in self.positions.items():
            if qty == 0 or "-FUT" in opt_symbol: continue
            meta = self.leg_metadata.get(opt_symbol)
            if not meta: continue
            
            d = self.bs.delta(spot, meta["strike"], self.expiry_years, r, self.iv, meta["otype"])
            net_delta += d * qty
        return net_delta

    def on_tick(self, symbol: str, data: dict) -> list | None:
        price = float(data['price'])
        asto = float(data.get("asto", 0.0))
        adx = float(data.get("adx", 0.0))
        s22 = float(data.get("whale_pivot", 0.0))
        r = 0.065 
        
        # ── [Hedge Hybrid] Extreme Trend Management ──
        if abs(asto) >= 90:
            net_delta = self._calculate_net_delta(price, r)
            
            # Whale Alignment Check
            is_aligned = (asto > 0 and s22 > 0) or (asto < 0 and s22 < 0)
            
            if is_aligned:
                # MAX CONFIDENCE: Neutralize Delta using Micro-Futures
                # Each Future has delta ~1.0. To neutralize net_delta, we need -net_delta futures.
                hedge_qty = round(-net_delta / 1.0) 
                if abs(hedge_qty) >= 1:
                    return [{
                        "action": "HEDGE_REQUEST", # Special action for Meta-Router
                        "symbol": f"{symbol}-FUT",
                        "quantity": abs(hedge_qty),
                        "side": "BUY" if hedge_qty > 0 else "SELL",
                        "hedge_label": f"HYBRID_{symbol}_{int(time.time())}",
                        "parent_uuid": getattr(self, "last_parent_uuid", "IC_GLOBAL"),
                        "reason": f"WHALE_ALIGNED_HEDGE: ASTO={asto:.1f}, S22={s22:.1f}, Delta={net_delta:.2f}"
                    }]
            else:
                # TRAP ALERT: Signal Liquidation to close the losing side
                return [{
                    "action": "TRAP_ALERT", # Handled by Liquidation Daemon
                    "symbol": symbol,
                    "side": "CALL" if asto > 0 else "PUT", # Losing side
                    "reason": f"WHALE_MISALIGNED_TRAP: ASTO={asto:.1f}, S22={s22:.1f}"
                }]
        
        # ── Standard Entry Logic ──
        # [Audit-Fix] Double-Key Authorization: Target S18 = 2 (Ranging)
        s18 = data.get("regime_s18", 0)
        if s18 != 2: return None
        
        T = self.expiry_years
        iv = self.iv
        
        # Dynamic Strike Selection
        s_put_wing = self.find_strike_for_delta(-self.wing_delta, price, T, r, iv, 'put')
        s_put_main = self.find_strike_for_delta(-self.spread_delta, price, T, r, iv, 'put')
        s_call_main = self.find_strike_for_delta(self.spread_delta, price, T, r, iv, 'call')
        s_call_wing = self.find_strike_for_delta(self.wing_delta, price, T, r, iv, 'call')
        
        parent_uuid = f"IC_{uuid.uuid4().hex[:8]}"
        self.last_parent_uuid = parent_uuid

        # Map strikes to symbols for metadata (Approximation for demo, real system uses search)
        # In a real run, the bridge/router updates leg_metadata upon execution.
        # Here we seed it to allow delta calculation.
        expiry = "26MAR" # Placeholder
        self.leg_metadata[f"{symbol}{expiry}{int(s_put_wing)}PE"] = {"strike": s_put_wing, "otype": "put"}
        self.leg_metadata[f"{symbol}{expiry}{int(s_put_main)}PE"] = {"strike": s_put_main, "otype": "put"}
        self.leg_metadata[f"{symbol}{expiry}{int(s_call_main)}CE"] = {"strike": s_call_main, "otype": "call"}
        self.leg_metadata[f"{symbol}{expiry}{int(s_call_wing)}CE"] = {"strike": s_call_wing, "otype": "call"}
        
        return [
            {"action": "BUY",  "otype": "PE", "strike": s_put_wing,  "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL"},
            {"action": "SELL", "otype": "PE", "strike": s_put_main,  "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL"},
            {"action": "SELL", "otype": "CE", "strike": s_call_main, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL"},
            {"action": "BUY",  "otype": "CE", "strike": s_call_wing, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL"}
        ]

class CreditSpreadStrategy(BaseStrategy):
    """Phase 7: Directional yield strategy using 2 option legs with dynamic strike selection."""
    def __init__(self, strategy_id: str, symbols: list[str], side: str = "bull", target_delta: float = 0.25, spread_width: float = 100, expiry_days: float = 7, iv: float = 0.15, **kwargs):
        super().__init__(strategy_id, symbols)
        self.side = side
        self.target_delta = float(target_delta)
        self.spread_width = float(spread_width)
        self.expiry_years = float(expiry_days) / 365.0
        self.iv = float(iv)
        self.bs = BlackScholes()

    def on_tick(self, symbol: str, data: dict) -> list | None:
        price = float(data['price'])
        T = self.expiry_years
        r = 0.065  # [D-02] Fixed: was self._redis.get() which crashes (BaseStrategy has no _redis)
        iv = self.iv
        parent_uuid = f"CS_{uuid.uuid4().hex[:8]}"

        if self.side == "bull": # Bull Put Spread
            otype = "PE"
            s1 = round((price - 100) / 50) * 50 # Fixed offset for basic CreditSpread
            s2 = s1 - self.spread_width
            return [
                {"action": "SELL", "otype": "PE", "strike": s1, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol},
                {"action": "BUY",  "otype": "PE", "strike": s2, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol}
            ]
        else: # Bear Call Spread
            otype = "CE"
            s1 = round((price + 100) / 50) * 50 
            s2 = s1 + self.spread_width
            return [
                {"action": "SELL", "otype": "CE", "strike": s1, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol},
                {"action": "BUY",  "otype": "CE", "strike": s2, "parent_uuid": parent_uuid, "lifecycle": "POSITIONAL", "underlying": symbol}
            ]

        return None

# Global state for dynamic strategies
active_strategies: dict[str, BaseStrategy] = {}

async def config_subscriber(redis_client):
    """Periodically polls Redis for strategy configurations."""
    logger.info("Starting dynamic configuration polling...")

    strategy_registry: dict[str, type[BaseStrategy]] = {
        "GammaScalping": GammaScalpingStrategy,
        "IronCondor": IronCondorStrategy,
        "CreditSpread": CreditSpreadStrategy,
        "DirectionalCredit": DirectionalCreditSpreadStrategy,
        "TastyTrade0DTE": TastyTrade0DTEStrategy,
        "ElasticHunter": ElasticHunterStrategy,
        "KineticHunter": KineticHunterStrategy,
        "PositionalHunter": PositionalHunterStrategy
    }

    while True:
        try:
            configs = await redis_client.hgetall("active_strategies")
            current_ids = set()
            
            for strat_id_b, config_raw in configs.items():
                strat_id = strat_id_b  # [D-03] Fixed: decode_responses=True already returns str
                current_ids.add(strat_id)
                config = json.loads(config_raw)
                
                if strat_id not in active_strategies:
                    if config.get('enabled', True):
                        logger.info(f"Loading strategy: {strat_id} ({config['type']})")
                        await redis_logger.log(f"Loading strategy {strat_id}", "SYSTEM")
                        try:
                            strat_type = config['type']
                            symbols = config.get('symbols', [])
                            cls = strategy_registry.get(strat_type)
                            if cls:
                                # Instantiate with kwargs to satisfy varied signatures
                                active_strategies[strat_id] = cls(strat_id, symbols, **config)
                                active_strategies[strat_id].schedule = config.get('schedule', {})
                                active_strategies[strat_id].execution_type = config.get('execution_type', 'Paper')
                            else:
                                logger.warning(f"Unknown strategy type: {strat_type}")
                        except Exception as e:
                            logger.error(f"Error initializing strategy {strat_id}: {e}")
                else:
                    # Strategy is already loaded, check if it was disabled
                    if not config.get('enabled', True):
                        logger.info(f"Unloading disabled strategy: {strat_id}")
                        await redis_logger.log(f"Disabling strategy {strat_id}", "SYSTEM")
                        active_strategies.pop(strat_id, None)
            
            deleted = set(active_strategies.keys()) - current_ids
            for d in deleted:
                logger.info(f"Unloading strategy: {d}")
                active_strategies.pop(d, None)
                
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Redis config polling error: {e}")
            await asyncio.sleep(5)

async def warmup_engine(active_strategies, num_ticks=1000):
    """Primes the engine with synthetic data to trigger JIT optimization."""
    logger.info(f"🚀 Starting Pre-Flight Warmup ({num_ticks} synthetic ticks)...")
    symbols = [
        "NIFTY50", "BANKNIFTY", "SENSEX",
        "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", 
        "SBIN", "AXISBANK", "KOTAKBANK", "LT", "RELIANCE"
    ]
    for _ in range(num_ticks):
        symbol = random.choice(symbols)
        tick_data = {
            "symbol": symbol,
            "price": random.uniform(20000, 25000) if "NIFTY" in symbol else random.uniform(2500, 3000),
            "volume": random.randint(1, 100),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        for strat in active_strategies.values():
            if symbol in strat.symbols:
                try: strat.on_tick(symbol, tick_data)
                except Exception: pass
    logger.info("✅ Warmup Complete. Bytecode is primed.")

async def _calibrate_vol_context(redis_client):
    """
    B2: Pre-market Rolling Z-Score normalization of the overnight gap.
    Computes VOL_GAP_Z:<symbol> for NIFTY50, BANKNIFTY, SENSEX based on
    prev_close vs first available tick. Writes result to Redis for the
    HMM engine to use when setting its first state threshold at 09:15.
    Called during the warm-up window (09:00–09:15).
    """
    symbols = ["NIFTY50", "BANKNIFTY", "SENSEX"]
    calibrated = 0
    for symbol in symbols:
        try:
            prev_close_raw = await redis_client.get(f"prev_close:{symbol}")
            if not prev_close_raw: continue
            prev_close = float(prev_close_raw)
            if prev_close <= 0: continue
 
            # Get most recent tick from short-lived tick history list
            tick_raw = await redis_client.lindex(f"tick_history:{symbol}", -1)
            if not tick_raw: continue
            tick = json.loads(tick_raw)
            open_price = float(tick.get("price", 0) or 0)
            if open_price <= 0: continue
 
            # 20-day rolling std (stored by market_sensor.py)
            rolling_std_raw = await redis_client.get(f"rolling_std_20d:{symbol}")
            rolling_std = float(rolling_std_raw or 50.0)  # fallback 50 points
            if rolling_std <= 0: rolling_std = 50.0
 
            gap_z = (open_price - prev_close) / rolling_std
            await redis_client.set(f"VOL_GAP_Z:{symbol}", round(gap_z, 4), ex=7200)  # 2h TTL
            logger.info(f"📊 Vol Gap Calibration [{symbol}]: Z={gap_z:.2f}")
            calibrated += 1
        except Exception as e:
            logger.warning(f"B2 calibration skipped for {symbol}: {e}")
 
    if calibrated > 0:
        logger.info(f"✅ B2 Vol Context: {calibrated}/{len(symbols)} symbols calibrated.")
    else:
        logger.warning("⚠️ B2 Vol Context: No symbols calibrated (prev_close data missing). Proceeding with defaults.")
 
 
async def run_strategies(sub_socket, push_socket, cmd_socket, mq_manager, redis_client, shm_ticks, shm_alpha_managers, shm_regime_managers, hedge_socket, asset_filter=None):
    """Subscribes to market data and runs strategies using SHM for zero-copy lookups."""
    logger.info("Starting Strategy Engine loop... (v5.5 Quantitative Risk Active)")
    
    shm_slots = {}
    last_shm_sync = 0
    # [F12-01] Cache HALT_KINETIC to avoid Redis call per tick*strategy
    halt_kinetic_cache = False
    halt_kinetic_last_check = 0
    # Wave 2: Cache MACRO_EVENT_LOCKDOWN
    macro_lockdown_cache = False
    macro_lockdown_last_check = 0
    # Wave 2: Cache IV for Low Vol Trap
    iv_cache = 15.0
    iv_last_check = 0
    
    # [Audit 14.5] Cache Lot Sizes from Redis
    lot_sizes_cache = {}
    lot_sizes_last_check = 0
    
    # Track real-time lot overrides from Meta-Router
    strategy_states = collections.defaultdict(lambda: {"lots": 1, "active": True})
    last_sequence_ids = collections.defaultdict(int)
    margin_manager = AsyncMarginManager(redis_client)
 
    async def handle_commands():
        nonlocal strategy_states
        while True:
            try:
                topic, cmd = await mq_manager.recv_json(cmd_socket)
                if cmd:
                    target = cmd.get("target")
                    command = cmd.get("command")
                    lots = cmd.get("lots", 1)
                    
                    if target == "ALL" or target is None:
                        for s_id in active_strategies:
                            strategy_states[s_id]["active"] = (command == "ACTIVATE")
                            strategy_states[s_id]["lots"] = lots
                    else:
                        strategy_states[target]["active"] = (command == "ACTIVATE")
                        strategy_states[target]["lots"] = lots
                        
            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Command Handler Error: {e}")
                await asyncio.sleep(0.1)
 
    # Launch command handler as a background task
    asyncio.create_task(handle_commands())
 
    while True:
        try:
            topic, tick_msg = await mq_manager.recv_json(sub_socket)
            if not tick_msg: continue

            # [Audit-Fix] Handle execution reports to update strategy position state
            if topic.startswith("EXEC."):
                qty = int(tick_msg.get("quantity", 0))
                action = tick_msg.get("action", "BUY")
                strat_id = tick_msg.get("strategy_id")
                symbol = tick_msg.get("symbol")
                change = qty if action == "BUY" else -qty
                
                if strat_id in active_strategies:
                    active_strategies[strat_id].positions[symbol] += change
                    logger.info(f"Position Updated: {strat_id} | {symbol} -> {active_strategies[strat_id].positions[symbol]}")
                continue
 
            # Periodically sync SHM slot mapping from Redis
            if time.time() - last_shm_sync > 10:
                shm_slots_raw = await redis_client.hgetall("shm_slots")
                shm_slots = {k: int(v) for k, v in shm_slots_raw.items()}  # [F1-02] decode_responses=True already returns str
                last_shm_sync = time.time()
 
            symbol = tick_msg.get("symbol")
            if asset_filter and symbol != asset_filter:
                # [Audit-Fix] Multi-Process Isolation: Skip if not the target asset
                continue
            
            # --- Zero-Copy SHM Access ---
            tick = tick_msg
            if shm_ticks and symbol in shm_slots:
                shm_tick = shm_ticks.read_tick(shm_slots[symbol])
                if shm_tick: tick = shm_tick  
            
            price = tick.get("price")
            curr_seq_id = tick.get("sequence_id", 0)
            
            # --- [Audit-Fix] Sequence Reset Detector ---
            last_seq_id = last_sequence_ids.get(symbol, 0)
            if curr_seq_id < last_seq_id:
                logger.warning(f"🔄 GATEWAY_RESTART detected for {symbol}: Sequence reset ({last_seq_id} -> {curr_seq_id}). Resetting internal trackers.")
                # Opportunity to reset sensitive indicators if needed
                last_sequence_ids[symbol] = curr_seq_id
            elif 0 < curr_seq_id <= last_seq_id:
                # Potential duplicate or out-of-order packet (if not a reset)
                # In a high-frequency context, we might skip this to avoid stale signals
                continue
            else:
                last_sequence_ids[symbol] = curr_seq_id

            # [Audit-Fix] Refresh Risk Caches & Get SHM Alpha (Per Asset)
            if time.time() - halt_kinetic_last_check > 5:
                halt_kinetic_val = await redis_client.get("HALT_KINETIC")
                halt_kinetic_cache = (halt_kinetic_val == "True")
                halt_kinetic_last_check = time.time()
            
            if time.time() - macro_lockdown_last_check > 5:
                lockdown_val = await redis_client.get("MACRO_EVENT_LOCKDOWN")
                macro_lockdown_cache = (lockdown_val == "True")
                macro_lockdown_last_check = time.time()
            
            if time.time() - iv_last_check > 5:
                iv_raw = await redis_client.get(f"atm_iv:{symbol}") or await redis_client.get("atm_iv")
                iv_cache = float(iv_raw or 0.15) * 100.0
                iv_last_check = time.time()

            if time.time() - lot_sizes_last_check > 60:
                lot_sizes_raw = await redis_client.hgetall("lot_sizes")
                if lot_sizes_raw:
                    lot_sizes_cache = {k: int(v) for k, v in lot_sizes_raw.items()}
                lot_sizes_last_check = time.time()

            # --- Zero-Copy SHM Access for Alpha & Regime (Per Asset) ---
            qstate = None
            regime = None
            if shm_alpha_managers and symbol in shm_alpha_managers:
                qstate = shm_alpha_managers[symbol].read()
            elif shm_alpha_managers and "GLOBAL" in shm_alpha_managers:
                qstate = shm_alpha_managers["GLOBAL"].read()

            if shm_regime_managers and symbol in shm_regime_managers:
                regime = shm_regime_managers[symbol].read()
            elif "BANKNIFTY" in symbol and "BANKNIFTY" in shm_regime_managers:
                regime = shm_regime_managers["BANKNIFTY"].read()
            elif "NIFTY" in symbol and "NIFTY50" in shm_regime_managers:
                regime = shm_regime_managers["NIFTY50"].read()
            
            # [Audit-Fix] Handshake Collision (Object vs Dict)
            s18_state = regime.regime_s18 if regime else 0
            s27_quality = regime.quality_s27 if regime else 0.0
            is_toxic = bool(qstate.veto) if qstate else False # SignalVector uses 'veto' field
            alpha_total = float(qstate.s_total) if qstate else 0.0
            asto_val = float(qstate.asto) if qstate else 0.0
            adx_val = float(qstate.adx) if qstate else 0.0
            vpin_val = float(qstate.vpin) if qstate else 0.0
            iv_rv_val = float(qstate.iv_rv_spread) if qstate else 0.0
            s26_val = float(regime.persistence_s26) if regime else 0.0
            sf_val = float(qstate.smart_flow) if qstate else 0.0
            hw_alphas = list(qstate.hw_alpha) if qstate else []

            # --- [Audit-Fix] Phasic Signal Decay (Latency-Weighted Alpha) ---
            latency_ms = float(tick.get("latency_ms", 0.0))
            if latency_ms > 50:
                # Alpha has a half-life. Decay starts after 50ms of lag.
                # Factor: e^(-0.005 * latency) -> 100ms lag = ~60% alpha retention
                decay_factor = math.exp(-0.005 * (latency_ms - 50))
                alpha_total *= decay_factor
                asto_val *= decay_factor
                if latency_ms > 200:
                    logger.warning(f"📉 ALPHA DECAY: {latency_ms:.0f}ms lag detected. Signal force reduced to {decay_factor:.1%} for {symbol}.")

            for strategy in list(active_strategies.values()):
                s_id = strategy.strategy_id
                if symbol not in strategy.symbols: continue  
                
                if not strategy.is_active_now() or not strategy_states[s_id]["active"]: continue
                
                # Veto entries if market is toxic OR alpha is severely negative
                if is_toxic and alpha_total < 0: continue

                # [C6-01] Check HALT_KINETIC: block all new entries after 15:00 IST
                if halt_kinetic_cache:
                    continue  # Block all new entries

                # Wave 2: Global Macro Event Lockdown enforcement
                if macro_lockdown_cache:
                    logger.warning(f"🚫 MACRO LOCKDOWN: {s_id} entry blocked.")
                    continue

                # Inject ASTO/ADX/S22/Regime for strategy visibility (Phase 5+)
                tick["asto"] = asto_val
                tick["adx"] = adx_val
                tick["whale_pivot"] = qstate.get("whale_pivot", 0.0) if qstate else 0.0
                tick["regime_s18"] = s18_state
                tick["quality_s27"] = s27_quality
                tick["persistence_s26"] = s26_val
                tick["iv_rv_spread"] = iv_rv_val
                tick["vpin"] = vpin_val
                tick["smart_flow"] = sf_val
                tick["hw_alphas"] = hw_alphas # [Audit-Fix] Passed for Power Five Gate
                tick["vwap"] = float(await redis_client.get(f"VWAP:{symbol}") or tick.get("price", 0.0)) # For Elastic exit
                tick["price_zscore"] = qstate.high_z if abs(qstate.high_z) > abs(qstate.low_z) else qstate.low_z
                
                # [Audit-Fix] Alpha-Death Exit Check
                exit_signal = strategy.check_exit(symbol, tick)
                if exit_signal:
                    signal = exit_signal
                else:
                    signal = strategy.on_tick(symbol, tick)

                if not signal: continue
                
                # Phase 7: Handle Multi-Leg (List) or Single-Leg (Str/Dict)
                signals = signal if isinstance(signal, list) else [signal]
                
                for leg in signals:
                    # [Hedge Hybrid] Route special actions to specialized handlers
                    if isinstance(leg, dict):
                        if leg.get("action") == "HEDGE_REQUEST":
                            await mq_manager.send_json(hedge_socket, Topics.HEDGE_REQUEST, leg)
                            continue
                        elif leg.get("action") == "TRAP_ALERT":
                            await redis_client.publish("panic_channel", json.dumps({
                                "action": "SQUARE_OFF_SIDE",
                                "symbol": leg["symbol"],
                                "side": leg["side"],
                                "reason": leg["reason"]
                            }))
                            continue

                    action = "WAIT"
                    qty = 1
                    
                    if isinstance(leg, str):
                        action = leg
                    elif isinstance(leg, dict):
                        action = leg.get("action", "WAIT")
                    else:
                        continue # Skip if leg is not str or dict
 
                    # Double-check signal against alpha bias
                    if action == "BUY" and alpha_total < -20: 
                        logger.warning(f"⚠️ VETO BUY: {s_id} signal rejected by negative Alpha ({alpha_total:.1f})")
                        continue
                    if action == "SELL" and alpha_total > 20:
                        logger.warning(f"⚠️ VETO SELL: {s_id} signal rejected by positive Alpha ({alpha_total:.1f})")
                        continue
                    
                    # Wave 2: Low Vol Trap (IV < 12%) for POSITIONAL trades
                    lifecycle = leg.get("lifecycle", "KINETIC") if isinstance(leg, dict) else "KINETIC"
                    if lifecycle == "POSITIONAL" and iv_cache < 12.0:
                        logger.warning(f"⚠️ LOW VOL TRAP: {s_id} POSITIONAL entry blocked (IV {iv_cache:.1f} < 12%)")
                        continue

                    # [C2-09] ASTO/Kinetic Filter: Block entry if |ASTO| <= 70 or ADX <= 25
                    if lifecycle == "KINETIC":
                        if abs(asto_val) <= 70 or adx_val <= 25:
                            logger.info(f"🛡️ ASTO KINETIC VETO: {s_id} entry blocked (ASTO={asto_val:.1f}, ADX={adx_val:.1f})")
                            continue

                    # [C2-10] ASTO/Kinetic Dynamic Exit: Force exit if |ASTO| < 50
                    current_pos = strategy.positions[symbol]
                    if lifecycle == "KINETIC" and current_pos != 0:
                        if abs(asto_val) < 50:
                            logger.critical(f"🏹 ASTO KINETIC EXIT: |{asto_val:.1f}| < 50. Closing {s_id} position for {symbol}.")
                            # Flip action to exit
                            action = "SELL" if current_pos > 0 else "BUY"
                            # Override leg to ensure it goes through
                            if isinstance(leg, dict): leg["action"] = action
                            else: leg = action
                
                    # --- Project K.A.R.T.H.I.K. Sizing Logic (Institutional Slider) ---
                    lots_from_router = strategy_states[s_id].get("lots", 1)
                    # Apply the S27 Quality Multiplier (0.0 to 1.0)
                    s27_mult = s27_quality / 100.0
                    qty = max(1, int(lots_from_router * s27_mult))
                    
                    # [Veto] If Quality is too low, block entry
                    if s27_quality < 30.0 and lifecycle != "POSITIONAL":
                        logger.warning(f"⚠️ QUALITY VETO: {s_id} rejected (S27 {s27_quality:.1f} < 30)")
                        continue
 
                    if isinstance(leg, str) and "QTY" in leg:
                        parts = leg.split("_")
                        if len(parts) >= 4:
                            action = parts[0]
                            try:
                                qty = int(parts[3])
                            except (ValueError, IndexError):
                                logger.warning(f"⚠️ Malformed signal QTY in {leg}. Using default/router qty.")
                        else:
                            logger.warning(f"⚠️ Signal {leg} missing parts. Expected at least 4.")
                        
                    # --- A2: Slippage Halt Guard ---
                    if await redis_client.get("SLIPPAGE_HALT") == "True":
                        logger.warning(f"⏸️ SLIPPAGE HALT ACTIVE: {s_id} entry blocked. Order book stabilizing.")
                        continue  # Skip dispatch while halt is active
 
                    # --- Atomic Capital Locking (SRS §2.4) ---
                    required_margin = price * qty
                    if action == "BUY":
                        if not await margin_manager.reserve(required_margin, strategy.execution_type):
                            logger.error(f"❌ MARGIN REJECTED: {s_id} needs ₹{required_margin:,.2f} but {strategy.execution_type} budget is exhausted.")
                            await redis_logger.log(f"Margin Reject: {s_id} needs ₹{required_margin:,.2f}", "RISK")
                            continue # Skip dispatch
 
                    order = {
                        "order_id": str(uuid.uuid4()),
                        "symbol": symbol,
                        "action": action,
                        "quantity": qty, 
                        "order_type": "MARKET",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "price": price,   
                        "inception_spot": 0.0, # Placeholder
                        "strategy_id": strategy.strategy_id,
                        "execution_type": strategy.execution_type,
                        "lifecycle_class": leg.get("lifecycle", "KINETIC") if isinstance(leg, dict) else "KINETIC",
                        "regime_snapshot": {
                            "s18": s18_state,
                            "s27": s27_quality,
                            "s26": s26_val,
                            "vpin": vpin_val,
                            "alpha": alpha_total,
                            "iv_rv": iv_rv_val,
                            "asto": asto_val,
                            "z_score": tick.get("price_zscore", 0.0),
                            "whale_pivot": tick.get("whale_pivot", 0.0)
                        }
                    }
                    
                    # [Audit-Fix] Baseline Spot Injection for Zero-DTE/Liquidation (Layer 6 Audit)
                    underlying = "NIFTY50"
                    if "BANKNIFTY" in symbol: underlying = "BANKNIFTY"
                    elif "SENSEX" in symbol: underlying = "SENSEX"
                    
                    if shm_ticks and underlying in SYMBOL_TO_SLOT:
                        idx_shm = shm_ticks.read_tick(SYMBOL_TO_SLOT[underlying])
                        if idx_shm:
                            order["inception_spot"] = float(idx_shm.get("price", 0.0))
                            logger.info(f"🎯 INCEPTION_SPOT CAPTURED: {underlying} @ {order['inception_spot']}")
                    
                    # Track dispatch time for phantom detection
                    dispatch_meta = order.copy()
                    dispatch_meta["dispatch_time_epoch"] = time.time()
                    dispatch_meta["broker_order_id"] = None
                    
                    await mq_manager.send_json(push_socket, Topics.ORDER_INTENT, order)
                    await redis_client.hset("pending_orders", order["order_id"], json.dumps(dispatch_meta, cls=NumpyEncoder))
                    
                    logger.info(f"DISPATCHED {action} {qty} {symbol} @ {price} | Lifecycle: {order['lifecycle_class']}")
                    
        except zmq.Again:
            continue
        except Exception as e:
            logger.error(f"Engine Loop Error: {e}")
            await asyncio.sleep(0.1)
 
async def start_engine():
    import argparse
    parser = argparse.ArgumentParser(description="Project K.A.R.T.H.I.K. Strategy Engine")
    parser.add_argument("--asset", type=str, help="Specific index to manage (e.g. NIFTY50)")
    parser.add_argument("--core", type=int, help="CPU core to bridge (Core Pinning)")
    parser.add_argument("--shadow", action="store_true", help="Run in shadow mode")
    args = parser.parse_args()

    # [Audit-Fix] Core Pinning for zero-jitter execution
    if args.core is not None:
        try:
            import psutil
            p = psutil.Process()
            p.cpu_affinity([args.core])
            logger.info(f"🎯 CORE PINNED: Strategy Engine locked to Core {args.core}")
        except Exception as e:
            logger.warning(f"⚠️ Core Pinning failed: {e}. psutil might be missing.")

    mq = MQManager()
    from core.auth import get_redis_url
    redis_url = get_redis_url()
    redis_client = redis.from_url(redis_url, decode_responses=True)
    
    # v5.5: Zero-latency Risk & Alpha via SHM (Per Asset) [Audit Fix]
    from core.shm import ShmManager, RegimeShm
    
    # [Audit-Fix] Multi-Process Isolation: Only load managers for the target asset
    target_assets = [args.asset] if args.asset else INDEX_METADATA.keys()
    shm_alpha_managers = {
        idx: ShmManager(asset_id=idx, mode='r') for idx in target_assets
    }
    shm_regime_managers = {
        idx: RegimeShm(asset_id=idx, mode='r') for idx in target_assets
    }
    shm_alpha_managers["GLOBAL"] = ShmManager(asset_id="GLOBAL", mode='r')
    
    try:
        shm_ticks = TickSharedMemory(create=False) # Read-only access
    except Exception as e:
        logger.warning(f"Shared Memory (Ticks) not found: {e}.")
        shm_ticks = None
    
    # [Audit 2.3] Add missing MQ topics so Engine receives all ticks
    sub_socket = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.", "EXEC."])
    push_socket = mq.create_push(Ports.ORDERS) # Pushes to Bridge
    # [Hedge Hybrid] Dedicated Hedge Request socket
    hedge_socket = mq.create_push(Ports.HEDGE_REQUEST)
    # [Audit 8.1] Bind CMD socket for controller push
    cmd_socket = mq.create_subscriber(Ports.SYSTEM_CMD, topics=["STRAT_", "ALL"], bind=True)
    
    # ── Phase 9: UI & Observability heartbeat ──
    from core.health import HeartbeatProvider
    hb = HeartbeatProvider("StrategyEngine", redis_client)
    asyncio.create_task(hb.run_heartbeat())

    # 1. Start Config Polling
    config_task = asyncio.create_task(config_subscriber(redis_client))
    
    # Wait for initial config to load
    await asyncio.sleep(1)
    
    # 2. B2: Overnight Gap Z-Score Calibration (before warmup)
    await _calibrate_vol_context(redis_client)
 
    # 3. Recommendation 2: Pre-Flight Warmup
    try:
        await run_strategies(sub_socket, push_socket, cmd_socket, mq, redis_client, shm_ticks, shm_alpha_managers, shm_regime_managers, hedge_socket, asset_filter=args.asset)
    finally:
        config_task.cancel()
        await redis_client.aclose()
        sub_socket.close()
        push_socket.close()
        hedge_socket.close()
        cmd_socket.close()
        mq.context.term()
 
if __name__ == "__main__":
    try:
        if uvloop: uvloop.install()
        elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) 
        asyncio.run(start_engine())
    except KeyboardInterrupt: pass
