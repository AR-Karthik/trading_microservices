import unittest
import asyncio
import json
import uuid
import time
from unittest.mock import MagicMock, AsyncMock, patch
from daemons.market_sensor import MarketSensor
from daemons.paper_bridge import execute_orders
from daemons.liquidation_daemon import LiquidationDaemon
from daemons.order_reconciler import OrderReconciler
from daemons.system_controller import SystemController

class TestMicrostructureRefinements(unittest.IsolatedAsyncioTestCase):

    # --- Market Sensor: Lee-Ready Classification ---
    def test_lee_ready_classification(self):
        sensor = MarketSensor(test_mode=True)
        
        # LTP > Mid (Buy)
        tick_buy = {"price": 100.5, "bid": 100.0, "ask": 100.2} # Mid = 100.1
        self.assertEqual(sensor._classify_trade(tick_buy), 1.0)
        
        # LTP < Mid (Sell)
        tick_sell = {"price": 99.8, "bid": 100.0, "ask": 100.2} # Mid = 100.1
        self.assertEqual(sensor._classify_trade(tick_sell), -1.0)
        
        # LTP == Mid (Neutral/Bounce)
        tick_mid = {"price": 100.1, "bid": 100.0, "ask": 100.2}
        self.assertEqual(sensor._classify_trade(tick_mid), 0.0)

    # --- Paper Bridge: Double-Tap Guard ---
    @patch('daemons.paper_bridge.MQManager')
    async def test_double_tap_guard(self, mock_mq_class):
        # Setup mocks
        mock_redis = AsyncMock()
        mock_redis.exists.return_value = False
        mock_redis.setex.return_value = True
        mock_redis.set.return_value = True
        
        mock_mq = MagicMock()
        mock_mq_class.return_value = mock_mq
        
        # recv_json must be an AsyncMock to be awaited
        mock_mq.recv_json = AsyncMock()
        
        order = {
            "symbol": "NIFTY_CE",
            "action": "BUY",
            "quantity": 1,
            "price": 100,
            "strategy_id": "GAMMA",
            "order_id": "test_order_1"
        }
        
        # Use side_effect to provide one order then exit
        mock_mq.recv_json.side_effect = [("ORDER.NIFTY_CE", order), asyncio.CancelledError()]
        
        mock_pool = MagicMock() # pool.acquire() is context manager
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        
        mock_pull_instance = MagicMock() # subscriber socket

        with patch('daemons.paper_bridge.calculate_slippage_and_fees', AsyncMock(return_value=(100.5, 20.0))):
            try:
                # Mocking internal Redis calls and DB calls
                mock_redis.get.return_value = None # global_risk
                mock_conn.fetchrow.return_value = {'realized_pnl': 0.0, 'quantity': 0}
                
                await execute_orders(mock_pull_instance, mock_pool, mock_mq, mock_redis)
            except asyncio.CancelledError:
                pass
        
        # Verify lock was set and journal was written
        mock_redis.setex.assert_called_with("lock:NIFTY_CE", 10, "LOCKED")
        mock_redis.set.assert_called()

    # --- Liquidation Daemon: Dynamic Slippage ---
    @patch('daemons.liquidation_daemon.redis.from_url')
    @patch('daemons.liquidation_daemon.MQManager')
    async def test_dynamic_slippage_panic(self, mock_mq_class, mock_redis_class):
        daemon = LiquidationDaemon()
        daemon._redis = AsyncMock()
        daemon._redis.get.side_effect = lambda k: "0.005" if k == "rv" else "20.0"
        daemon._redis.delete.return_value = True
        daemon.order_pub = MagicMock()
        
        # Add a position
        symbol = "NIFTY_PE"
        daemon.orphaned_positions[symbol] = {
            "symbol": symbol,
            "quantity": 50,
            "price": 100.0,
            "action": "BUY",
            "entry_time": time.time(),
            "execution_type": "Paper"
        }
        
        tick = {"price": 95.0, "bid": 94.8, "ask": 95.2}
        state = {}
        
        with patch.object(daemon, '_attempt_exit', new_callable=AsyncMock) as mock_exit:
            await daemon._evaluate_barriers(symbol, tick, state)
            
            mock_exit.assert_called()
            args = mock_exit.call_args[1]
            self.assertIn("PANIC_VOL_EXIT", args['reason'])
            self.assertAlmostEqual(args['price'], 93.85, places=1)

    # --- System Controller: Pending Journal Audit ---
    @patch('daemons.system_controller.redis.from_url')
    async def test_boot_audit(self, mock_redis_class):
        ctrl = SystemController()
        ctrl.redis = AsyncMock()
        # In IsolatedAsyncioTestCase, ctrl.redis.keys will be awaited.
        ctrl.redis.keys.return_value = ["Pending_Journal:order_123"]
        ctrl.redis.get.return_value = json.dumps({"symbol": "NIFTY", "action": "BUY"})
        
        with patch.object(ctrl, '_telegram_alert', AsyncMock()) as mock_tel:
            await ctrl._audit_pending_journal()
            mock_tel.assert_called()
            self.assertIn("ORPHANED INTENT", mock_tel.call_args[0][0])

if __name__ == '__main__':
    unittest.main()
