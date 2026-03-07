import pytest
from daemons.paper_bridge import calculate_slippage_and_fees

@pytest.mark.asyncio
async def test_dynamic_slippage_buy():
    """
    Verifies that the Buy execution price includes the 50% spread penalty.
    """
    order = {"action": "BUY", "price": 20000.0}
    # Spread = 20000 * 0.0005 = 10
    # Penalty = 10 * 0.5 = 5
    # Expected Price = 20005.0
    
    exec_price, fees = await calculate_slippage_and_fees(order)
    
    assert exec_price == 20005.0
    assert fees == 20.0

@pytest.mark.asyncio
async def test_dynamic_slippage_sell():
    """
    Verifies that the Sell execution price includes the 50% spread penalty.
    """
    order = {"action": "SELL", "price": 100.0}
    # Spread = 100 * 0.0005 = 0.05
    # Penalty = 0.05 * 0.5 = 0.025
    # Expected Price = 100 - 0.025 = 99.975
    
    exec_price, fees = await calculate_slippage_and_fees(order)
    
    assert exec_price == 99.975
    assert fees == 20.0
