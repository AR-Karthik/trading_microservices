import pytest
import asyncio
from daemons.meta_router import MetaRouter

@pytest.mark.asyncio
async def test_kill_switch_override():
    router = MetaRouter(test_mode=True)
    
    # Case 1: Extreme spread vacuum
    state_toxic = {"hurst": 0.6, "rv": 0.001, "ofi": 10.0, "spread_z": 4.5}
    kill_reason = router.check_kill_switches(state_toxic)
    assert "HALT: Macro Shock" in kill_reason
    
    # Verify that it produces LIQUIDATE commands
    commands = await router.broadcast("BULL", kill_reason=kill_reason)
    assert any(cmd['command'] == "ORPHAN" for cmd in commands)

@pytest.mark.asyncio
async def test_regime_detection():
    router = MetaRouter(test_mode=True)
    
    # Trending regime
    state_trend = {"hurst": 0.75, "rv": 0.001, "ofi": 5.0, "spread_z": 0.5}
    regime = router.detect_regime(state_trend)
    assert regime == "TREND"
    
    # Range regime
    state_range = {"hurst": 0.3, "rv": 0.0001, "ofi": 0.1, "spread_z": 0.2}
    regime = router.detect_regime(state_range)
    assert regime == "CHOP"
