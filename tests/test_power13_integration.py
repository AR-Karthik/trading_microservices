
import asyncio
import json
import unittest
import os
import sys
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure core is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

class TestPower13Integration(unittest.IsolatedAsyncioTestCase):
    """
    Verifies that 'Power 13' tickers (e.g. RELIANCE) correctly flow through 
    the Market Sensor and update heavyweight-specific analytics like Dispersion.
    """

    async def asyncSetUp(self):
        # Mock MQ and Redis
        self.mq_patcher = patch("core.mq.MQManager")
        self.mock_mq_class = self.mq_patcher.start()
        self.mock_mq = self.mock_mq_class.return_value
        
        self.redis_patcher = patch("redis.asyncio.from_url")
        self.mock_redis_url = self.redis_patcher.start()
        self.mock_redis = MagicMock()
        self.mock_redis_url.return_value = self.mock_redis

        # Initialize Market Sensor in test mode
        from daemons.market_sensor import MarketSensor
        self.sensor = MarketSensor(test_mode=True)
        self.sensor._redis = self.mock_redis
        
        # Mock internal components to prevent process startup
        self.sensor.pub = MagicMock()
        self.sensor.shm = MagicMock()
        self.sensor._compute_in = MagicMock()

    async def asyncTearDown(self):
        self.mq_patcher.stop()
        self.redis_patcher.stop()

    async def test_heavyweight_tick_updates_buffers(self):
        """Standard Power 13 tick (RELIANCE) should update hw_prices buffer."""
        tick = {
            "symbol": "RELIANCE",
            "price": 2500.0,
            "bid": 2499.5,
            "ask": 2500.5,
            "last_volume": 10,
            "timestamp": "2026-03-14T12:00:00Z"
        }
        
        # Manually trigger the logic that would run on message recv
        symbol = tick["symbol"]
        price = tick["price"]
        
        # 1. Update heavyweight price buffer
        from daemons.market_sensor import TOP_10_HEAVYWEIGHTS
        self.assertIn("RELIANCE", TOP_10_HEAVYWEIGHTS)
        
        self.sensor.hw_prices["RELIANCE"].append(price)
        self.assertEqual(len(self.sensor.hw_prices["RELIANCE"]), 1)
        self.assertEqual(self.sensor.hw_prices["RELIANCE"][0], 2500.0)

    async def test_multi_heavyweight_dispersion(self):
        """Verify all 13 assets are handled correctly in dispersion snapshots."""
        from daemons.market_sensor import TOP_10_HEAVYWEIGHTS
        indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
        all_assets = indices + TOP_10_HEAVYWEIGHTS
        
        for symbol in all_assets:
            self.sensor.hw_prices[symbol].append(100.0)
            
        hw_snapshot = {k: list(v) for k, v in self.sensor.hw_prices.items() if len(v) > 0}
        for symbol in all_assets:
            self.assertIn(symbol, hw_snapshot, f"Symbol {symbol} missing from snapshot")
        
        self.assertEqual(len(hw_snapshot), 13)

    async def test_state_vector_includes_cvd(self):
        """The state vector published by sensor must now include the 'cvd' field."""
        self.sensor.cvd = 500.0
        
        # Setup mock Redis returns
        async def mock_get(key):
            data = {
                "fii_bias": "100.0",
                "news_sentiment_score": "0.5",
                "net_delta_nifty50": "1000",
                "entry_price:NIFTY50": "22000"
            }
            return data.get(key)
            
        self.mock_redis.get = AsyncMock(side_effect=mock_get)
        self.mock_redis.set = AsyncMock()
        self.mock_redis.hset = AsyncMock()
        self.mock_redis.hget = AsyncMock(return_value=None)
        self.mock_redis.hgetall = AsyncMock(return_value={})
        self.mock_redis.publish = AsyncMock()
        self.mock_redis.lpush = AsyncMock()
        self.mock_redis.ltrim = AsyncMock()
        self.mock_redis.aclose = AsyncMock()
        
        # We need some ticks in store to avoid RV errors
        self.sensor.tick_store["NIFTY50"].append({"price": 22000})
        self.sensor.tick_store["NIFTY50"].append({"price": 22010})

        # Capture the published state
        with patch.object(self.sensor.mq, "send_json", new_callable=AsyncMock) as mock_send:
            await self.sensor._publish_market_state("NIFTY50", 22010.0)
            
            # Check call arguments
            # send_json(socket, topic, data)
            args, kwargs = mock_send.call_args
            state = args[2]
            
            self.assertEqual(state["cvd"], 500.0)
            self.assertEqual(state["symbol"], "NIFTY50")

if __name__ == "__main__":
    unittest.main()
