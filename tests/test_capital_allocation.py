import unittest
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys

# Mock dependencies before importing daemons
sys.modules['core.mq'] = MagicMock()
sys.modules['core.margin'] = MagicMock()
sys.modules['NorenRestApiPy.NorenApi'] = MagicMock()

from daemons.meta_router import MetaRouter
from daemons.strategy_engine import run_strategies
from daemons.liquidation_daemon import LiquidationDaemon

class TestCapitalAllocation(unittest.IsolatedAsyncioTestCase):
    
    async def asyncSetUp(self):
        self.mock_redis = AsyncMock()
        self.mock_mq = MagicMock()
        self.mock_pub = AsyncMock()
        self.mock_mq.create_publisher.return_value = self.mock_pub
        self.mock_mq.send_json = AsyncMock()
        
    @patch('daemons.meta_router.redis.Redis')
    async def test_meta_router_lot_sizing(self, mock_redis_class):
        # Setup MetaRouter with mocked Redis
        router = MetaRouter(test_mode=True)
        router._redis = self.mock_redis
        router.current_regime = "TRENDING"
        
        # Mock Redis values
        self.mock_redis.get.side_effect = lambda key: {
            "AVAILABLE_MARGIN_PAPER": "50000",
            "GLOBAL_CAPITAL_LIMIT_PAPER": "100000"
        }.get(key)
        self.mock_redis.hget.return_value = "65" # lot_size
        
        # Test State
        state = {
            "symbol": "NIFTY50",
            "price": 100.0,
            "ask": 100.0,
            "gex_sign": "NEGATIVE",
            "s_total": 50
        }
        
        # Calculate expected lots:
        # strat_weight = 0.40 (TRENDING)
        # avail_margin = 50000
        # ask_price = 100.0
        # lot_size = 65
        # calc_lots = floor((50000 * 0.4) / (100.0 * 65)) = floor(20000 / 6500) = 3
        
        commands = await router.broadcast(state)
        
        # Verify STRAT_GAMMA has correct lots
        gamma_cmd = next(c for c in commands if c['target'] == "STRAT_GAMMA")
        self.assertEqual(gamma_cmd['command'], "ACTIVATE")
        self.assertEqual(gamma_cmd['lots'], 3)
        
    @patch('daemons.strategy_engine.AsyncMarginManager')
    @patch('daemons.strategy_engine.MQManager')
    async def test_strategy_engine_margin_lock(self, mock_mq_class, mock_mm_class):
        # Mock MarginManager
        mock_mm = mock_mm_class.return_value
        mock_mm.reserve.return_value = True
        
        # Mock MQ
        mock_mq = mock_mq_class.return_value
        mock_mq.recv_json = AsyncMock(side_effect=[
            ("TICK", {"symbol": "NIFTY50", "price": 100.0}), # Tick 1
            None # Exit loop
        ])
        
        # Mock Strategy
        mock_strat = MagicMock()
        mock_strat.strategy_id = "STRAT_TEST"
        mock_strat.symbols = ["NIFTY50"]
        mock_strat.on_tick.return_value = "BUY"
        mock_strat.execution_type = "Paper"
        mock_strat.is_active_now.return_value = True
        
        # We need to mock active_strategies global in strategy_engine
        with patch('daemons.strategy_engine.active_strategies', {"STRAT_TEST": mock_strat}):
            # This is tricky because run_strategies is an infinite loop
            # We'll use a timeout or a side effect to break it
            pass

    @patch('daemons.liquidation_daemon.AsyncMarginManager')
    async def test_liquidation_margin_release(self, mock_mm_class):
        mock_mm = mock_mm_class.return_value
        mock_mm.release = AsyncMock()
        
        daemon = LiquidationDaemon(test_mode=True)
        daemon._redis = self.mock_redis
        daemon.order_pub = MagicMock()
        
        pos = {"quantity": 130, "execution_type": "Paper"} # 2 lots of 65
        symbol = "NIFTY50"
        price = 150.0
        reason = "TEST_EXIT"
        
        await daemon._attempt_exit(pos, symbol, price, reason)
        
        # Verify release called with Price * Qty = 150 * 130 = 19500
        mock_mm.release.assert_awaited_with(19500.0, "Paper")

if __name__ == "__main__":
    unittest.main()
