import pytest
import asyncio
import json
from unittest.mock import AsyncMock, patch, MagicMock
from daemons.live_bridge import LiveExecutionEngine

@pytest.mark.asyncio
async def test_kill_switch_trigger(mock_mq, mock_pool, mock_redis, mock_shoonya):
    """
    Validates that the Daily Loss Kill Switch triggers once the loss limit is hit.
    """
    # Initialize engine with a -1000 limit
    with patch('daemons.live_bridge.NorenApi', return_value=mock_shoonya):
        engine = LiveExecutionEngine(mock_mq, mock_pool, mock_redis)
        engine.daily_loss_limit = -1000.0
        
        # Simulating realized P&L drops to -1500
        engine.total_realized_pnl = -1500.0
        
        await engine.check_kill_switch()
        
        assert engine.is_kill_switch_triggered is True
        # Verify panic signal was published to Redis
        mock_redis.publish.assert_called()
        args = mock_redis.publish.call_args[0]
        assert args[0] == "panic_channel"
        payload = json.loads(args[1])
        assert payload["action"] == "SQUARE_OFF"
        assert payload["reason"] == "KILL_SWITCH"

@pytest.mark.asyncio
async def test_order_blocked_by_kill_switch(mock_mq, mock_pool, mock_redis, mock_shoonya):
    """
    Ensures that no orders are handled once the kill switch is triggered.
    """
    with patch('daemons.live_bridge.NorenApi', return_value=mock_shoonya):
        engine = LiveExecutionEngine(mock_mq, mock_pool, mock_redis)
        engine.is_kill_switch_triggered = True
        
        order = {"action": "BUY", "quantity": 100, "symbol": "NIFTY"}
        await engine.handle_order(order, MagicMock())
        
        # Shoonya API should NOT be called
        mock_shoonya.place_order.assert_not_called()
