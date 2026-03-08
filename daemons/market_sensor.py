import asyncio
import json
import logging
import collections
import numpy as np
import polars as pl
from datetime import datetime, timezone
import redis
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MarketSensor")

class CompositeAlphaScorer:
    def __init__(self):
        self.weights = {"env": 0.20, "str": 0.30, "div": 0.50}
        
    def get_total_score(self, env_data, str_data, div_data):
        # Time Multiplier: 1.0 during peak, 0.5 during lunch/EOD
        now = datetime.now()
        hour = now.hour + now.minute/60.0
        multiplier = 1.0
        if (11.5 <= hour <= 13.5) or (hour >= 15.0):
            multiplier = 0.5
            
        s_env = self._calc_env(env_data)
        s_str = self._calc_str(str_data)
        s_div = self._calc_div(div_data)
        
        total = ((self.weights['env'] * s_env) + 
                 (self.weights['str'] * s_str) + 
                 (self.weights['div'] * s_div)) * multiplier
        return np.clip(total, -100, 100)

    def _calc_env(self, data):
        # FII Bias, VIX Momentum, IVP
        score = 0
        score += data.get('fii_bias', 0) # EOD modifier
        score += -10 * data.get('vix_slope', 0) # Inverse correlation
        if data.get('ivp', 50) > 80: return -100 # Nullify long
        if data.get('ivp', 50) < 20: score += 20
        return score

    def _calc_str(self, data):
        # Basis, Max Pain, OI Wall, PCR
        score = 0
        score += 50 * data.get('basis_slope', 0)
        score += -5 * data.get('dist_max_pain', 0)
        pcr = data.get('pcr', 1.0)
        if pcr > 1.3: score -= 20
        if pcr < 0.7: score += 20
        return score

    def _calc_div(self, data):
        # Price vs PCR/VIX/CVD
        score = 0
        if data.get('price_slope', 0) > 0 and data.get('pcr_slope', 0) < 0:
            score -= 50 # Bearish divergence
        if data.get('price_slope', 0) < 0 and data.get('cvd_slope', 0) > 0:
            score += 50 # Bullish divergence
        return score

class MarketSensor:
    def __init__(self, test_mode=False):
        self.mq = MQManager()
        self.test_mode = test_mode
        self.scorer = CompositeAlphaScorer()
        if not test_mode:
            self.pub = self.mq.create_publisher(Ports.MARKET_STATE)
            self.redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        self.tick_store = collections.defaultdict(lambda: collections.deque(maxlen=2000))
        
    def calculate_hurst(self, series):
        if len(series) < 50: return 0.5
        lags = range(2, 20)
        tau = [np.sqrt(np.std(np.subtract(series[lag:], series[:-lag]))) for lag in lags]
        poly = np.polyfit(np.log(lags), np.log(tau), 1)
        return poly[0] * 2.0

    async def run(self):
        logger.info("Market Sensor Active. Subscribing to TICK data...")
        sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA])
        
        while True:
            try:
                topic, tick = await self.mq.recv_json(sub)
                if tick:
                    symbol = tick.get("symbol", "NIFTY50")
                    self.tick_store[symbol].append(tick)
                    
                    if len(self.tick_store[symbol]) > 100:
                        df = pl.DataFrame(list(self.tick_store[symbol]))
                        prices = df.select("price").to_numpy().flatten()
                        
                        # Calculate components for Scorer
                        env_data = {'fii_bias': 10, 'vix_slope': 0.01, 'ivp': 25}
                        str_data = {'basis_slope': 0.02, 'dist_max_pain': 10, 'pcr': 0.85}
                        div_data = {'price_slope': 0.01, 'pcr_slope': 0.02, 'cvd_slope': 0.01}
                        
                        s_total = self.scorer.get_total_score(env_data, str_data, div_data)
                        
                        state = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "s_total": float(s_total),
                            "hurst": float(self.calculate_hurst(prices[-500:])),
                            "rv": float(np.std(np.diff(np.log(prices)))),
                            "time_of_day": datetime.now().strftime("%H:%M:%S")
                        }
                        
                        await self.mq.send_json(self.pub, state, topic=Topics.MARKET_STATE)
                        self.redis.set("latest_market_state", json.dumps(state))
                        logger.info(f"ALPHA_SCORE: {s_total:.2f}")

                await asyncio.sleep(0.01)

            except Exception as e:
                logger.error(f"Error in MarketSensor: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    sensor = MarketSensor()
    asyncio.run(sensor.run())
