import asyncio
import logging
import uuid
import collections
import json
import random
import time
import sys
import pandas as pd
from redis import asyncio as redis
from datetime import datetime, timezone
from core.mq import MQManager, Ports, RedisLogger
from core.greeks import BlackScholes
from core.shared_memory import TickSharedMemory

try:
    import uvloop
except ImportError:
    uvloop = None

logger = logging.getLogger("StrategyEngine")
redis_logger = RedisLogger()

# --- Recommendation 7: Shadow Trading Flag ---
IS_SHADOW_MODE = "--shadow" in sys.argv

class BaseStrategy:
    def __init__(self, strategy_id, symbols):
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.positions = collections.defaultdict(int)
        # Using a fixed size deque for history to avoid memory growth
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=100))
        self.schedule = {}
        self.execution_type = "Paper" # Default to paper trading
        self.is_shadow = False # Default to not shadow trading

    def on_tick(self, symbol: str, data: dict) -> str | None:
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

class SMACrossoverStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbols, period=10):
        super().__init__(strategy_id, symbols)
        self.period = period

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = data['price']
        self.history[symbol].append(price)
        
        hist = list(self.history[symbol])
        if len(hist) < self.period:
            return None
            
        # Use simple mean of last N periods
        sma = sum(list(hist)[-self.period:]) / self.period # type: ignore
        current_pos = self.positions[symbol]
        
        if price > sma * 1.0005 and current_pos <= 0:
            self.positions[symbol] = 1
            return "BUY"
        elif price < sma * 0.9995 and current_pos >= 0:
            self.positions[symbol] = -1
            return "SELL"
        return None

class MeanReversionStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbols, period=5, threshold=0.001):
        super().__init__(strategy_id, symbols)
        self.period = period
        self.threshold = threshold

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = data['price']
        self.history[symbol].append(price)
        
        hist = list(self.history[symbol])
        period = int(self.period) # Cast to int
        if len(hist) < period:
            return None
            
        mean = sum(hist[-period:]) / period # Use last 'period' elements for mean
        current_pos = int(self.positions[symbol]) # Ensure pos is int
        
        if price < mean * (1 - self.threshold) and current_pos <= 0:
            self.positions[symbol] = 1
            return "BUY"
        elif price > mean * (1 + self.threshold) and current_pos >= 0:
            self.positions[symbol] = -1
            return "SELL"
        return None

class OIPulseScalpingStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbols, oi_threshold=2.0):
        super().__init__(strategy_id, symbols)
        self.oi_threshold = oi_threshold

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
    def __init__(self, strategy_id, symbols, anchor_time="09:15:00"):
        super().__init__(strategy_id, symbols)
        self.anchor_time = anchor_time
        self.tick_data = collections.defaultdict(list)

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = data['price']
        volume = data.get('volume', 1) 
        now = datetime.now()
        
        self.tick_data[symbol].append({
            'time': now, 'price': price, 'volume': volume
        })
        
        df = pd.DataFrame(self.tick_data[symbol])
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
    def __init__(self, strategy_id, symbols, strike=22000, expiry_days=30, iv=0.15, r=0.07, hedge_threshold=0.10):
        super().__init__(strategy_id, symbols)
        self.strike = strike
        self.expiry_years = expiry_days / 365.0
        self.iv = iv
        self.r = r
        self.hedge_threshold = hedge_threshold
        self.last_calc_time = datetime.now(timezone.utc)

    def on_tick(self, symbol: str, data: dict) -> str | None:
        price = data['price']
        
        # 1. Update time to expiry (decay over time)
        now = datetime.now(timezone.utc)
        dt = (now - self.last_calc_time).total_seconds() / (365.0 * 24 * 3600)
        self.expiry_years -= dt
        self.last_calc_time = now
        
        if self.expiry_years <= 0: return None
        
        # 2. Calculate Option Delta (assuming Long Call)
        bs = BlackScholes()
        delta = bs.delta(price, self.strike, self.expiry_years, self.r, self.iv, "call")
        
        # 3. Hedge logic: Position should be -delta * 100
        pos = self.positions[symbol]
        target_pos = -int(delta * 100)
        error = target_pos - pos
        
        if abs(error) >= self.hedge_threshold:
            return "BUY" if error > 0 else "SELL"
        return None

class CustomCodeStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbols, code_str):
        super().__init__(strategy_id, symbols)
        self.code_str = code_str
        self.compiled_env = {}
        
        try:
            exec(self.code_str, {}, self.compiled_env)
        except Exception as e:
            logger.error(f"Failed to compile custom strategy {strategy_id}: {e}")

    def on_tick(self, symbol: str, data: dict) -> str | None:
        if 'on_tick' not in self.compiled_env:
            return None
            
        price = data['price']
        self.history[symbol].append(price)
        hist = list(self.history[symbol])
        current_position = self.positions[symbol]
        
        try:
            # Custom code expected signature: on_tick(symbol, history, position, price)
            signal = self.compiled_env['on_tick'](symbol, hist, current_position, price)
            
            if signal == "BUY":
                self.positions[symbol] += 1
            elif signal == "SELL":
                self.positions[symbol] -= 1
                
            return signal
        except Exception as e:
            logger.error(f"Error executing custom strategy {self.strategy_id} on {symbol}: {e}")
            return None

# Global state for dynamic strategies
active_strategies: dict[str, BaseStrategy] = {}

async def config_subscriber(redis_client):
    """Periodically polls Redis for strategy configurations."""
    global active_strategies
    logger.info("Starting dynamic configuration polling...")

    strategy_registry = {
        "SMA": SMACrossoverStrategy,
        "MeanReversion": MeanReversionStrategy,
        "OIPulse": OIPulseScalpingStrategy,
        "GammaScalping": GammaScalpingStrategy,
        "AnchoredVWAP": AnchoredVWAPStrategy,
        "Custom": CustomCodeStrategy
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
                            
                            if strat_type in strategy_registry:
                                cls = strategy_registry[strat_type]
                                if strat_type == "SMA":
                                    active_strategies[strat_id] = cls(strat_id, symbols, period=config.get('period', 10))
                                elif strat_type == "MeanReversion":
                                    active_strategies[strat_id] = cls(strat_id, symbols, period=config.get('period', 5), threshold=config.get('threshold', 0.001))
                                elif strat_type == "OIPulse":
                                    active_strategies[strat_id] = cls(strat_id, symbols, oi_threshold=config.get('oi_threshold', 2.0))
                                elif strat_type == "GammaScalping":
                                    active_strategies[strat_id] = cls(
                                        strat_id, symbols, 
                                        strike=float(config.get('strike', 22000)),
                                        expiry_days=float(config.get('expiry_days', 30)),
                                        iv=float(config.get('iv', 0.15)),
                                        r=float(config.get('r', 0.07)),
                                        hedge_threshold=float(config.get('hedge_threshold', 0.10))
                                    )
                                elif strat_type == "AnchoredVWAP":
                                    active_strategies[strat_id] = cls(strat_id, symbols, anchor_time=config.get('anchor_time', "09:15:00"))
                                elif strat_type == "Custom":
                                    active_strategies[strat_id] = cls(strat_id, symbols, config.get('code', ''))
                                
                                # Attach schedule metadata to the instance
                                active_strategies[strat_id].schedule = config.get('schedule', {})
                                active_strategies[strat_id].execution_type = config.get('execution_type', 'Paper')
                            else:
                                logger.warning(f"Unknown strategy type: {strat_type}")
                        except Exception as e:
                            logger.error(f"Error initializing strategy {strat_id}: {e}")
                    else:
                        pass # Strategy exists in config but is disabled
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
    logger.info(f"ðŸš€ Starting Pre-Flight Warmup ({num_ticks} synthetic ticks)...")
    
    # Representative tickers for warmup
    symbols = ["NIFTY50", "BANKNIFTY", "RELIANCE"]
    
    for _ in range(num_ticks):
        symbol = random.choice(symbols)
        tick_data = {
            "symbol": symbol,
            "price": random.uniform(20000, 25000) if "NIFTY" in symbol else random.uniform(2500, 3000),
            "volume": random.randint(1, 100),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # We manually call on_tick for all strategies that watch these symbols
        for strat in active_strategies.values():
            if symbol in strat.symbols:
                # We ignore signals during warmup
                try:
                    strat.on_tick(symbol, tick_data)
                except Exception:
                    pass
                    
    logger.info("âœ… Warmup Complete. Bytecode is primed.")

async def run_strategies(sub_socket, push_socket, mq_manager, redis_client, shm):
    """Subscribes to market data and runs strategies using SHM for zero-copy lookups."""
    logger.info("Starting Strategy Engine loop...")
    
    shm_slots = {}
    last_shm_sync = 0
    
    while True:
        try:
            topic, tick_msg = await mq_manager.recv_json(sub_socket)
            if not tick_msg: continue

            # Periodically sync SHM slot mapping from Redis
            if time.time() - last_shm_sync > 10:
                shm_slots = await redis_client.hgetall("shm_slots")
                # Convert bytes keys/values to str/int
                shm_slots = {k.decode(): int(v) for k, v in shm_slots.items()}
                last_shm_sync = time.time()

            symbol = tick_msg.get("symbol")
            
            # --- Recommendation 1: Zero-Copy Shared Memory Access ---
            # Instead of just using tick_msg, we pull from SHM for the latest state
            tick = tick_msg
            if symbol in shm_slots:
                shm_tick = shm.read_tick(shm_slots[symbol])
                if shm_tick:
                    tick = shm_tick  # Use the SHM data which is potentially newer/faster to read
            
            price = tick.get("price")
            
            for strategy in list(active_strategies.values()):
                if symbol not in strategy.symbols:
                    continue  
                
                if not strategy.is_active_now():
                    continue
                    
                signal = strategy.on_tick(symbol, tick)
                
                if signal:
                    action = signal
                    # --- Dynamic Sizing based on DTE ---
                    now_dt = datetime.now()
                    # DTE 0/1 (Wed/Thu) = 100%, Others = 50%
                    is_expiry = now_dt.strftime("%A") in ["Wednesday", "Thursday"]
                    base_qty = 100
                    qty = base_qty if is_expiry else int(base_qty * 0.5)
                    if "QTY" in signal:
                        parts = signal.split("_")
                        action = parts[0]
                        qty = int(parts[3])
                        
                    # --- Hard Global Budget Check ---
                    required_margin = price * qty
                    # Create generic temporary margin manager or import it at top?
                    # We should use an instance created outside the loop, but for now we'll import here if needed.
                    # Wait, we need to import MarginManager. I will update the top of the file separately, or use a passed-in margin_manager.
                    if action == "BUY":
                        from core.margin import AsyncMarginManager
                        margin_manager = AsyncMarginManager(redis_client)
                        if not await margin_manager.reserve(required_margin, strategy.execution_type):
                            logger.error(f"❌ MARGIN REJECTED: {symbol} needs ₹{required_margin:,.2f} but {strategy.execution_type} budget is exhausted.")
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
                    
                    push_socket.send_json(order)
                    await redis_client.hset("pending_orders", order["order_id"], json.dumps(dispatch_meta))
                    
                    logger.info(f"DISPATCHED {action} {qty} {symbol} @ {price}")
                    
        except Exception as e:
            if "Resource temporarily unavailable" in str(e):
                continue
            logger.error(f"Engine Loop Error: {e}")
            await asyncio.sleep(0.1)

async def start_engine():
    mq = MQManager()
    redis_client = redis.Redis(host='localhost', port=6379, db=0)
    shm = TickSharedMemory(create=False) # Read-only access
    
    sub_socket = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK."])
    push_socket = mq.create_push(Ports.ORDERS)
    
    # 1. Start Config Polling
    config_task = asyncio.create_task(config_subscriber(redis_client))
    
    # Wait for initial config to load
    await asyncio.sleep(1)
    
    # 2. Recommendation 2: Pre-Flight Warmup
    await warmup_engine(active_strategies)

    try:
        await run_strategies(sub_socket, push_socket, mq, redis_client, shm)
    finally:
        config_task.cancel()
        shm.close()
        await redis_client.aclose()
        sub_socket.close()
        push_socket.close()
        mq.context.term()

if __name__ == "__main__":
    try:
        if uvloop:
            uvloop.install()
        elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) # type: ignore
        asyncio.run(start_engine())
    except KeyboardInterrupt:
        pass
