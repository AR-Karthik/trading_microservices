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

class MarketSensor:
    def __init__(self, test_mode=False):
        self.mq = MQManager()
        self.test_mode = test_mode
        if not test_mode:
            self.pub = self.mq.create_publisher(Ports.MARKET_STATE)
            self.redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        self.tick_store = collections.defaultdict(lambda: collections.deque(maxlen=2000))
        
    def calculate_hurst(self, series):
        """Simple R/S analysis for Hurst Exponent."""
        if len(series) < 50: return 0.5
        lags = range(2, 20)
        tau = [np.sqrt(np.std(np.subtract(series[lag:], series[:-lag]))) for lag in lags]
        poly = np.polyfit(np.log(lags), np.log(tau), 1)
        return poly[0] * 2.0

    def calculate_rv(self, prices):
        """Realized Volatility calculation."""
        if len(prices) < 2: return 0.0
        log_returns = np.diff(np.log(prices))
        return np.std(log_returns)

    def calculate_ofi(self, df):
        """Order Flow Imbalance calculation."""
        if df.is_empty(): return 0.0
        # OFI = (sum of volume * sign of price change)
        return df.select(
            (pl.col("volume") * pl.col("price").diff().fill_null(0).sign()).sum()
        ).item() or 0.0

    async def run(self):
        logger.info("Market Sensor Active. Subscribing to TICK data...")
        sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA])
        
        while True:
            try:
                topic, tick = await self.mq.recv_json(sub)
                if tick:
                    symbol = tick.get("symbol", "NIFTY50")
                    self.tick_store[symbol].append(tick)
                    
                    # Calculate State every 500ms (approx)
                    if len(self.tick_store[symbol]) > 100:
                        df = pl.DataFrame(list(self.tick_store[symbol]))
                        
                        # RV: 15-min rolling std dev of 1-sec log returns
                        df = df.with_columns([
                            (pl.col("price").log().diff()).alias("log_ret")
                        ])
                        rv = df.select(pl.col("log_ret").std()).to_numpy()[0][0] or 0.0
                        
                        # OFI
                        ofi = self.calculate_ofi(df.tail(100))
                        
                        # Hurst
                        prices = df.select("price").to_numpy().flatten()
                        hurst = self.calculate_hurst(prices[-500:])
                        
                        # Options metrics (Placeholder: In real system, pull from Option Chain data)
                        iv = 15.0 # Mock
                        skew = 1.2 # Mock
                        
                        state = {
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "rv": float(rv),
                            "ofi": float(ofi),
                            "hurst": float(hurst),
                            "iv": float(iv),
                            "skew": float(skew),
                            "spread_z": 0.5, # Mock
                            "book_depth": 0.8, # Mock
                            "time_of_day": datetime.now().strftime("%H:%M:%S")
                        }
                        
                        await self.mq.send_json(self.pub, state, topic=Topics.MARKET_STATE)
                        self.redis.set("latest_market_state", json.dumps(state))
                        logger.debug(f"STATE: H={hurst:.2f} RV={rv:.6f} OFI={ofi:.2f}")

                await asyncio.sleep(0.01)

            except Exception as e:
                logger.error(f"Error in MarketSensor: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    sensor = MarketSensor()
    asyncio.run(sensor.run())
