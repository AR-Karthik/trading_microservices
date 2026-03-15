import asyncio
import logging
import uuid
import collections
import json
import random
import time
import sys
import pandas as pd
import os
from redis import asyncio as redis
from datetime import datetime, timezone
from core.logger import setup_logger
from core.mq import MQManager, Ports, RedisLogger, Topics
from core.greeks import BlackScholes
import re
from core.shared_memory import TickSharedMemory
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



class OIPulseScalpingStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, symbols: list[str], oi_threshold: float = 2.0, **kwargs):
        super().__init__(strategy_id, symbols)
        self.oi_threshold = float(oi_threshold)

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = data['price']
        current_oi = data.get('oi', 0)
        prev_oi = data.get('prev_oi', current_oi)
        
        if prev_oi == 0: return None
        
        oi_change_pct = (current_oi - prev_oi) / prev_oi * 100
        
        pos = self.positions[symbol]
        if oi_change_pct > self.oi_threshold and pos <= 0:
            return "BUY"
        elif oi_change_pct < -self.oi_threshold and pos >= 0:
            return "SELL"
        return None

class AnchoredVWAPStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, symbols: list[str], anchor_time: str = "09:15:00", **kwargs):
        super().__init__(strategy_id, symbols)
        self.anchor_time = anchor_time
        # [Audit 12.1] Bound the VWAP tick storage
        self.tick_data = collections.defaultdict(lambda: collections.deque(maxlen=10000))

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = data['price']
        volume = data.get('volume', 1) 
        now = datetime.now()
        
        self.tick_data[symbol].append({
            'time': now, 'price': price, 'volume': volume
        })
        
        df = pd.DataFrame(list(self.tick_data[symbol]))
        anchor_dt = datetime.combine(now.date(), datetime.strptime(self.anchor_time, "%H:%M:%S").time())
        df = df[df['time'] >= anchor_dt]
        
        if df.empty: return None
        
        vwap = (df['price'] * df['volume']).sum() / df['volume'].sum()
        
        pos = self.positions[symbol]
        if price > vwap and pos <= 0:
            return "BUY"
        elif price < vwap and pos >= 0:
            return "SELL"
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
        
        if self.expiry_years <= 0: return None
        
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
    def __init__(self, strategy_id: str, symbols: list[str], wing_delta: float = 0.10, spread_delta: float = 0.25, expiry_days: float = 7, iv: float = 0.15, **kwargs):
        super().__init__(strategy_id, symbols)
        self.wing_delta = float(wing_delta)
        self.spread_delta = float(spread_delta)
        self.expiry_years = float(expiry_days) / 365.0
        self.iv = float(iv)
        self.bs = BlackScholes()

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
        return round(mid / 50) * 50 # Snap to 50-point strike

    def on_tick(self, symbol: str, data: dict) -> list | None:
        # Only enter in 'High Volatility Chop' regime or similar
        price = float(data['price'])
        if not data.get('is_ranging', False): return None
        
        T = self.expiry_years
        r = 0.065 # Standardized Risk-free rate (Audit 5.1)
        iv = self.iv
        
        # Dynamic Strike Selection
        s_put_wing = self.find_strike_for_delta(-self.wing_delta, price, T, r, iv, 'put')
        s_put_main = self.find_strike_for_delta(-self.spread_delta, price, T, r, iv, 'put')
        s_call_main = self.find_strike_for_delta(self.spread_delta, price, T, r, iv, 'call')
        s_call_wing = self.find_strike_for_delta(self.wing_delta, price, T, r, iv, 'call')

        parent_uuid = f"IC_{uuid.uuid4().hex[:8]}"
        
        return [
            {"action": "BUY",  "otype": "PE", "strike": s_put_wing,  "parent_uuid": parent_uuid},
            {"action": "SELL", "otype": "PE", "strike": s_put_main,  "parent_uuid": parent_uuid},
            {"action": "SELL", "otype": "CE", "strike": s_call_main, "parent_uuid": parent_uuid},
            {"action": "BUY",  "otype": "CE", "strike": s_call_wing, "parent_uuid": parent_uuid}
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
        r = float(self._redis.get("CONFIG:RISK_FREE_RATE") or 0.065)
        iv = self.iv
        parent_uuid = f"CS_{uuid.uuid4().hex[:8]}"

        if self.side == "bull": # Bull Put Spread
            otype = "PE"
            s1 = round((price * 0.98) / 50) * 50 # Approximation for speed or use find_strike
            s2 = s1 - self.spread_width
            return [
                {"action": "SELL", "otype": "PE", "strike": s1, "parent_uuid": parent_uuid},
                {"action": "BUY",  "otype": "PE", "strike": s2, "parent_uuid": parent_uuid}
            ]
        else: # Bear Call Spread
            s1 = round((price * 1.02) / 50) * 50
            s2 = s1 + self.spread_width
            return [
                {"action": "SELL", "otype": "CE", "strike": s1, "parent_uuid": parent_uuid},
                {"action": "BUY",  "otype": "CE", "strike": s2, "parent_uuid": parent_uuid}
            ]

# Global state for dynamic strategies
active_strategies: dict[str, BaseStrategy] = {}

async def config_subscriber(redis_client):
    """Periodically polls Redis for strategy configurations."""
    logger.info("Starting dynamic configuration polling...")

    strategy_registry: dict[str, type[BaseStrategy]] = {
        "OIPulse": OIPulseScalpingStrategy,
        "GammaScalping": GammaScalpingStrategy,
        "AnchoredVWAP": AnchoredVWAPStrategy,
        "IronCondor": IronCondorStrategy,
        "CreditSpread": CreditSpreadStrategy
    }

    while True:
        try:
            configs = await redis_client.hgetall("active_strategies")
            current_ids = set()
            
            for strat_id_b, config_raw in configs.items():
                strat_id = strat_id_b.decode('utf-8')
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
 
 
async def run_strategies(sub_socket, push_socket, cmd_socket, mq_manager, redis_client, shm_ticks, shm_alpha):
    """Subscribes to market data and runs strategies using SHM for zero-copy lookups."""
    logger.info("Starting Strategy Engine loop... (v5.5 Quantitative Risk Active)")
    
    shm_slots = {}
    last_shm_sync = 0
    
    # Track real-time lot overrides from Meta-Router
    strategy_states = collections.defaultdict(lambda: {"lots": 1, "active": True})
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
                        
            except Exception as e:
                logger.error(f"Command Handler Error: {e}")
                await asyncio.sleep(0.1)
 
    # Launch command handler as a background task
    asyncio.create_task(handle_commands())
 
    while True:
        try:
            topic, tick_msg = await mq_manager.recv_json(sub_socket)
            if not tick_msg: continue
 
            # Periodically sync SHM slot mapping from Redis
            if time.time() - last_shm_sync > 10:
                shm_slots_raw = await redis_client.hgetall("shm_slots")
                shm_slots = {k.decode(): int(v) for k, v in shm_slots_raw.items()}
                last_shm_sync = time.time()
 
            symbol = tick_msg.get("symbol")
            
            # --- Zero-Copy SHM Access ---
            tick = tick_msg
            if shm_ticks and symbol in shm_slots:
                shm_tick = shm_ticks.read_tick(shm_slots[symbol])
                if shm_tick: tick = shm_tick  
            
            price = tick.get("price")
            
            # --- v5.5: Zero-Latency Risk Veto ---
            qstate = shm_alpha.read() if shm_alpha else None
            is_toxic = qstate.get("toxic_veto", False) if qstate else False
            alpha_total = qstate.get("s_total", 0.0) if qstate else 0.0
 
            for strategy in list(active_strategies.values()):
                s_id = strategy.strategy_id
                if symbol not in strategy.symbols: continue  
                
                if not strategy.is_active_now() or not strategy_states[s_id]["active"]: continue
                
                # Veto entries if market is toxic OR alpha is severely negative
                if is_toxic and alpha_total < 0: continue
 
                signal = strategy.on_tick(symbol, tick)
                if not signal: continue
                
                # Phase 7: Handle Multi-Leg (List) or Single-Leg (Str/Dict)
                signals = signal if isinstance(signal, list) else [signal]
                
                for leg in signals:
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
                
                    # --- Project K.A.R.T.H.I.K. Sizing Logic ---
                    # Prioritize Meta-Router calculated lots, fallback to DTE logic
                    lots_from_router = strategy_states[s_id].get("lots")
                    if lots_from_router and lots_from_router > 0:
                        qty = lots_from_router
                    else:
                        # Fallback sizing based on DTE
                        now_dt = datetime.now()
                        # DTE 0/1 (Wed/Thu) = 100%, Others = 50%
                        is_expiry = now_dt.strftime("%A") in ["Wednesday", "Thursday"]
                        base_qty = 100
                        qty = base_qty if is_expiry else int(base_qty * 0.5)
 
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
                        "strategy_id": strategy.strategy_id,
                        "execution_type": strategy.execution_type
                    }
                    
                    # Track dispatch time for phantom detection
                    dispatch_meta = order.copy()
                    dispatch_meta["dispatch_time_epoch"] = time.time()
                    dispatch_meta["broker_order_id"] = None
                    
                    await mq_manager.send_json(push_socket, Topics.ORDER_INTENT, order)
                    await redis_client.hset("pending_orders", order["order_id"], json.dumps(dispatch_meta, cls=NumpyEncoder))
                    
                    logger.info(f"DISPATCHED {action} {qty} {symbol} @ {price}")
                    
        except Exception as e:
            if "Resource temporarily unavailable" in str(e):
                continue
            logger.error(f"Engine Loop Error: {e}")
            await asyncio.sleep(0.1)
 
async def start_engine():
    mq = MQManager()
    redis_host = os.getenv("REDIS_HOST", "localhost")
    # [Audit 14.4] Use from_url for true async context manager compatibility
    redis_client = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
    
    # v5.5: Zero-latency Risk & Alpha via SHM
    from core.shm import ShmManager
    shm_alpha = ShmManager(mode='r')
    
    try:
        shm_ticks = TickSharedMemory(create=False) # Read-only access
    except Exception as e:
        logger.warning(f"Shared Memory (Ticks) not found: {e}.")
        shm_ticks = None
    
    # [Audit 2.3] Add missing MQ topics so Engine receives all ticks
    sub_socket = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.", "EXEC."])
    push_socket = mq.create_push(Ports.ORDERS) # Pushes to Bridge
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
    await warmup_engine(active_strategies)
 
    try:
        await run_strategies(sub_socket, push_socket, cmd_socket, mq, redis_client, shm_ticks, shm_alpha)
    finally:
        config_task.cancel()
        await redis_client.aclose()
        sub_socket.close()
        push_socket.close()
        cmd_socket.close()
        mq.context.term()
 
if __name__ == "__main__":
    try:
        if uvloop: uvloop.install()
        elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) 
        asyncio.run(start_engine())
    except KeyboardInterrupt: pass
