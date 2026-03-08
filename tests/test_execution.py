import pytest
from daemons.paper_bridge import calculate_slippage_and_fees

@pytest.mark.asyncio
async def test_dynamic_slippage_buy():
    """
    Verifies that the Buy execution price includes the 0.3-0.5 spread penalty.
    """
    order = {"action": "BUY", "price": 20000.0}
    
    exec_price, fees = await calculate_slippage_and_fees(order)
    
    assert 20000.3 <= exec_price <= 20000.5
    assert fees == 20.0

@pytest.mark.asyncio
async def test_dynamic_slippage_sell():
    """
    Verifies that the Sell execution price includes the 0.3-0.5 spread penalty.
    """
    order = {"action": "SELL", "price": 100.0}
    
    exec_price, fees = await calculate_slippage_and_fees(order)
    
    assert 99.5 <= exec_price <= 99.7
    assert fees == 20.0
