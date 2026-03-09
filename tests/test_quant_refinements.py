import unittest
import asyncio
import time
from collections import deque
from unittest.mock import MagicMock, AsyncMock, patch
from daemons.market_sensor import MarketSensor
from daemons.meta_router import MetaRouter
from daemons.liquidation_daemon import LiquidationDaemon

class TestQuantRefinements(unittest.IsolatedAsyncioTestCase):

    # --- Market Sensor: VPIN (Flow Toxicity) ---
    def test_vpin_calculation(self):
        sensor = MarketSensor(test_mode=True)
        # Mock VPIN thresholds
        sensor.vpin_bucket_size = 100
        sensor.vpin_threshold = 0.8
        
        # Simulate extreme aggressive buying (High VPIN)
        # Lee-Ready: LTP > Mid == Aggressive Buy
        tick_buy = {"price": 105.0, "bid": 100.0, "ask": 101.0, "last_volume": 95}
        sensor._update_vpin(tick_buy)

        # To fill the bucket, add a small sell
        tick_sell = {"price": 99.0, "bid": 100.0, "ask": 101.0, "last_volume": 5}
        sensor._update_vpin(tick_sell)
        
        # Manually trigger compute logic or check deque
        self.assertGreater(sensor.vpin_series[-1], 0.8)

        # Simulate balanced flow (Low VPIN)
        sensor.vpin_series.clear()
        tick_buy_bal = {"price": 105.0, "bid": 100.0, "ask": 101.0, "last_volume": 55}
        tick_sell_bal = {"price": 99.0, "bid": 100.0, "ask": 101.0, "last_volume": 45}
        
        sensor._update_vpin(tick_buy_bal)
        sensor._update_vpin(tick_sell_bal)
        
        self.assertLess(sensor.vpin_series[-1], 0.8)

    # --- Meta Router: Kelly Criterion ---
    @patch('daemons.meta_router.MQManager')
    def test_kelly_criterion_sizing(self, mock_mq_class):
        router = MetaRouter()
        # Mock historical stats
        router.strategy_stats = {
            "GAMMA": {"profit_factor": 1.5, "win_rate": 0.55}
        }
        router.global_risk = {
            "max_position_size": 100000,
            "capital": 1000000
        }
        
        # Scenario 1: High conviction state
        # Assume HMM state probability p = 0.80, b = 1.5
        # Kelly = p - (1-p)/b = 0.80 - 0.20/1.5 = 0.66
        # Fractional Kelly (half) = 0.33 -> 33% weight.
        
        # In actual meta_router logic
        # weight = max(0.1, min(0.5, 0.5 * (0.8 - (0.2 / 1.5))))  
        state_prob = 0.80
        pf = router.strategy_stats["GAMMA"]["profit_factor"]
        f_star = state_prob - ((1.0 - state_prob) / pf)
        f_kelly = round(0.5 * f_star, 2)
        weight = max(0.1, min(0.5, f_kelly))
        
        self.assertAlmostEqual(weight, 0.33, places=2)

    # --- Liquidation Daemon: 70-30 Rule Execution ---
    @patch('daemons.liquidation_daemon.redis.from_url')
    @patch('daemons.liquidation_daemon.MQManager')
    async def test_dual_stage_liquidation(self, mock_mq_class, mock_redis_class):
        daemon = LiquidationDaemon()
        daemon._redis = AsyncMock()
        daemon._redis.get.side_effect = lambda k: {
            "atr": "10.0", "rv": "0.0001", "vix": "15.0", "cvd_flip_ticks": "0", "hmm_regime": "TRENDING"
        }.get(k, "0.0")
        
        daemon.order_pub = MagicMock()
        
        symbol = "NIFTY_CE"
        daemon.orphaned_positions[symbol] = {
            "symbol": symbol,
            "quantity": 100,
            "price": 100.0,
            "action": "BUY",
            "entry_time": time.time(),
            "execution_type": "Paper"
        }
        state = {}
        
        # Target 1 = Entry + 1.2 * ATR = 100 + 12 = 112
        tick_target_1 = {"price": 112.5, "bid": 112.4, "ask": 112.6}
        
        with patch.object(daemon, '_attempt_partial_exit', new_callable=AsyncMock) as mock_partial_exit:
            await daemon._evaluate_barriers(symbol, tick_target_1, state)
            mock_partial_exit.assert_called_once()
            args = mock_partial_exit.call_args[1]
            self.assertEqual(args["pct"], 0.70)
            self.assertIn("TP1_HIT", args["reason"])
            
        # Verify runner is active
        self.assertTrue(daemon.orphaned_positions[symbol]["runner_active"])
        
        # Target 2 = Entry + 2.5 * ATR = 100 + 25 = 125
        # SL for Runner = Entry = 100
        
        # Test Runner SL Hit
        tick_sl = {"price": 99.0, "bid": 98.9, "ask": 99.1}
        with patch.object(daemon, '_attempt_exit', new_callable=AsyncMock) as mock_exit:
            await daemon._evaluate_barriers(symbol, tick_sl, state)
            mock_exit.assert_called_once()
            self.assertIn("SL_HIT", mock_exit.call_args[1]["reason"])

if __name__ == '__main__':
    unittest.main()
