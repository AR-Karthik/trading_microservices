import asyncio
import logging
import uuid
import collections
import json
import random
from redis import asyncio as redis
from datetime import datetime, timezone
from core.mq import MQManager, Ports, RedisLogger
from core.greeks import BlackScholes

logger = logging.getLogger("StrategyEngine")
redis_logger = RedisLogger()

class BaseStrategy:
    def __init__(self, strategy_id, symbols):
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.positions: dict[str, int] = collections.defaultdict(int)
        # Using a fixed size deque for history to avoid memory growth
        self.history: dict[str, collections.deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=100))

    def on_tick(self, symbol, data):
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
        start = self.schedule.get('start', "00:00")
        end = self.schedule.get('end', "23:59")
        
        if days and current_day not in days:
            return False
            
        if not (start <= current_time <= end):
            return False
            
        return True

class SMACrossoverStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbols, period=10):
        super().__init__(strategy_id, symbols)
        self.period = period

    def on_tick(self, symbol, data):
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

    def on_tick(self, symbol, data):
        price = data['price']
        self.history[symbol].append(price)
        
        hist = list(self.history[symbol])
        if len(hist) < self.period:
            return None
            
        mean = sum(list(hist)[-self.period:]) / self.period # type: ignore
        current_pos = self.positions[symbol]
        
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

    def on_tick(self, symbol, data):
        price = data['price']
        current_oi = data.get('oi', 0)
        prev_oi = data.get('prev_oi', current_oi)
        self.history[symbol].append(price)
        
        hist = list(self.history[symbol])
        current_pos = self.positions[symbol]
        
        # 1. Calculate OI Change Percentage
        oi_change_pct = ((current_oi - prev_oi) / prev_oi) * 100 if prev_oi > 0 else 0
        
        # 2. Check for Price Momentum (Simple 3-tick trend)
        is_trending_up = all(x < price for x in hist[-4:-1]) if len(hist) > 4 else False # type: ignore

        # 3. Decision Logic: Go long on OI surge + price uptrend
        if current_pos <= 0 and oi_change_pct > self.oi_threshold and is_trending_up:
            self.positions[symbol] += 1
            return "BUY"
        
        # Exit if OI starts dropping (unwinding)
        if current_pos > 0 and (oi_change_pct < -0.5):
            self.positions[symbol] -= 1
            return "SELL"

        return None

class AnchoredVWAPStrategy(BaseStrategy):
    def __init__(self, strategy_id, symbols, anchor_time="09:15:00"):
        super().__init__(strategy_id, symbols)
        self.anchor_time = anchor_time
        # history for VWAP needs [time, high, low, close, volume]
        self.tick_data: dict[str, list] = collections.defaultdict(list)

    def on_tick(self, symbol, data):
        price = data['price']
        volume = data.get('volume', 1) # Fallback to 1 if no volume provided
        now = datetime.now()
        
        self.tick_data[symbol].append({
            'timestamp': now,
            'high': price, 'low': price, 'close': price, 'volume': volume
        })
        
        # Calculate Anchored VWAP
        df = pd.DataFrame(self.tick_data[symbol])
        if df.empty: return None
        
        from datetime import time as dt_time
        import pandas as pd
        anchor_t = dt_time.fromisoformat(self.anchor_time)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        mask = df['timestamp'].dt.time >= anchor_t
        anchor_df = df.loc[mask].copy()
        
        if anchor_df.empty: return None
        
        anchor_df['tp'] = (anchor_df['high'] + anchor_df['low'] + anchor_df['close']) / 3
        anchor_df['pv'] = anchor_df['tp'] * anchor_df['volume']
        
        avwap = anchor_df['pv'].cumsum().iloc[-1] / anchor_df['volume'].cumsum().iloc[-1]
        
        current_pos = self.positions[symbol]
        if price > avwap * 1.001 and current_pos <= 0:
            self.positions[symbol] = 1
            return "BUY"
        elif price < avwap * 0.999 and current_pos >= 0:
            self.positions[symbol] = -1
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

    def on_tick(self, symbol, data):
        price = data['price']
        
        # 1. Update time to expiry (decay over time)
        now = datetime.now(timezone.utc)
        elapsed = (now - self.last_calc_time).total_seconds() / (365 * 24 * 3600)
        self.expiry_years = max(0.00001, self.expiry_years - elapsed)
        self.last_calc_time = now

        # 2. Calculate Option Delta (assuming Long Call for this scalper)
        opt_delta = BlackScholes.delta(price, self.strike, self.expiry_years, self.r, self.iv, option_type='call')
        
        # 3. Delta Neutrality check: Position in Futures should = -Option Delta * 100 (for lot size 100)
        # For simplicity, we manage 1 contract (1 lot = 100 qty)
        target_futures_qty = -int(opt_delta * 100)
        current_futures_qty = self.positions[symbol]
        
        delta_error = target_futures_qty - current_futures_qty
        
        if abs(delta_error) >= (self.hedge_threshold * 100):
            # Trigger hedge
            qty = abs(delta_error)
            action = "BUY" if delta_error > 0 else "SELL"
            self.positions[symbol] += delta_error
            return f"{action}_FUTURES_QTY_{qty}"

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

    def on_tick(self, symbol, data):
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
                                # ... existing initialization ...
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

async def run_strategies(sub_socket, push_socket, mq_manager):
    """Subscribes to market data and runs strategies."""
    logger.info("Starting Strategy Engine loop...")
    
    while True:
        try:
            topic, tick = await mq_manager.recv_json(sub_socket)
            if not tick: continue

            symbol = tick.get("symbol")
            price = tick.get("price")
            
            for strategy in list(active_strategies.values()):
                if symbol not in strategy.symbols:
                    continue  
                
                if not strategy.is_active_now():
                    continue
                    
                signal = strategy.on_tick(symbol, tick)
                
                if signal:
                    action = signal
                    qty = 100
                    if "QTY" in signal:
                        parts = signal.split("_")
                        action = parts[0]
                        qty = int(parts[3])
                        
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
                    await mq_manager.send_json(push_socket, order)
                    logger.info(f"Signal: {strategy.strategy_id} -> {action} {qty} {symbol}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in strategy loop: {e}")
            await asyncio.sleep(1)

async def start_engine():
    mq = MQManager()
    redis_client = redis.Redis(host='localhost', port=6379, db=0)
    sub_socket = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK."])
    push_socket = mq.create_push(Ports.ORDERS)
    config_task = asyncio.create_task(config_subscriber(redis_client))

    try:
        await run_strategies(sub_socket, push_socket, mq)
    finally:
        config_task.cancel()
        await redis_client.aclose()
        sub_socket.close()
        push_socket.close()
        mq.context.term()

if __name__ == "__main__":
    try:
        if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) # type: ignore
        asyncio.run(start_engine())
    except KeyboardInterrupt:
        pass
