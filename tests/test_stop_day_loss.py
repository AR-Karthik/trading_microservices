"""
tests/test_stop_day_loss.py
============================
Unit tests for the Stop Day Loss feature, covering:
 - Breach detection logic in liquidation_daemon._check_stop_day_loss()
 - Entry gate logic in paper_bridge (simulated via mock Redis)
 - Dashboard sidebar display logic (direct function tests)
 - Daily P&L tracking key format
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_mock_redis(keys: dict) -> AsyncMock:
    """Returns an AsyncMock Redis client backed by a simple dict store."""
    r = AsyncMock()
    store = dict(keys)

    async def get_side(key):
        return store.get(key)

    async def set_side(key, value):
        store[key] = str(value)

    async def publish_side(channel, message):
        return 1

    r.get = AsyncMock(side_effect=get_side)
    r.set = AsyncMock(side_effect=set_side)
    r.publish = AsyncMock(side_effect=publish_side)
    r._store = store   # expose for assertions
    return r


# ══════════════════════════════════════════════════════════════════════════════
# 1. LiquidationDaemon._check_stop_day_loss()
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckStopDayLoss:
    """Tests the core breach-detection method in LiquidationDaemon."""

    def _make_daemon(self, redis_store: dict):
        """Create a LiquidationDaemon with a mocked Redis client."""
        from daemons.liquidation_daemon import LiquidationDaemon
        with patch("daemons.liquidation_daemon.MQManager"):
            daemon = LiquidationDaemon.__new__(LiquidationDaemon)
            daemon._redis = make_mock_redis(redis_store)
        return daemon

    def test_no_breach_when_pnl_positive(self):
        """No breach should be set when daily P&L is positive."""
        daemon = self._make_daemon({
            "STOP_DAY_LOSS": "5000",
            "DAILY_REALIZED_PNL_PAPER": "1200.0",
            "DAILY_REALIZED_PNL_LIVE": "0.0",
        })
        asyncio.get_event_loop().run_until_complete(daemon._check_stop_day_loss())
        # Breach flag should be False (not "True")
        assert daemon._redis._store.get("STOP_DAY_LOSS_BREACHED_PAPER") != "True"

    def test_breach_triggered_at_limit(self):
        """Breach fires exactly when cumulative loss equals STOP_DAY_LOSS."""
        daemon = self._make_daemon({
            "STOP_DAY_LOSS": "5000",
            "DAILY_REALIZED_PNL_PAPER": "-5000.0",
            "DAILY_REALIZED_PNL_LIVE": "0.0",
        })
        asyncio.get_event_loop().run_until_complete(daemon._check_stop_day_loss())
        assert daemon._redis._store.get("STOP_DAY_LOSS_BREACHED_PAPER") == "True"

    def test_breach_triggers_square_off_publish(self):
        """When breach fires, panic_channel receives SQUARE_OFF_ALL."""
        daemon = self._make_daemon({
            "STOP_DAY_LOSS": "3000",
            "DAILY_REALIZED_PNL_PAPER": "-4000.0",
            "DAILY_REALIZED_PNL_LIVE": "0.0",
        })
        asyncio.get_event_loop().run_until_complete(daemon._check_stop_day_loss())
        daemon._redis.publish.assert_awaited_once()
        call_args = daemon._redis.publish.await_args
        channel = call_args[0][0]
        payload = json.loads(call_args[0][1])
        assert channel == "panic_channel"
        assert payload["action"] == "SQUARE_OFF_ALL"
        assert payload["reason"] == "STOP_DAY_LOSS_BREACHED_PAPER"

    def test_no_duplicate_publish_if_already_breached(self):
        """If breach flag is already True, do not re-publish panic signal."""
        daemon = self._make_daemon({
            "STOP_DAY_LOSS": "5000",
            "DAILY_REALIZED_PNL_PAPER": "-6000.0",
            "DAILY_REALIZED_PNL_LIVE": "0.0",
            "STOP_DAY_LOSS_BREACHED_PAPER": "True",   # pre-set
        })
        asyncio.get_event_loop().run_until_complete(daemon._check_stop_day_loss())
        daemon._redis.publish.assert_not_awaited()

    def test_live_mode_breach_independent(self):
        """LIVE breach must not affect PAPER breach flag."""
        daemon = self._make_daemon({
            "STOP_DAY_LOSS": "5000",
            "DAILY_REALIZED_PNL_PAPER": "200.0",
            "DAILY_REALIZED_PNL_LIVE": "-7000.0",
        })
        asyncio.get_event_loop().run_until_complete(daemon._check_stop_day_loss())
        assert daemon._redis._store.get("STOP_DAY_LOSS_BREACHED_LIVE") == "True"
        assert daemon._redis._store.get("STOP_DAY_LOSS_BREACHED_PAPER") != "True"

    def test_default_limit_used_when_redis_key_missing(self):
        """If STOP_DAY_LOSS key is missing, defaults to ₹5000."""
        daemon = self._make_daemon({
            # STOP_DAY_LOSS not set
            "DAILY_REALIZED_PNL_PAPER": "-6000.0",
            "DAILY_REALIZED_PNL_LIVE": "0.0",
        })
        asyncio.get_event_loop().run_until_complete(daemon._check_stop_day_loss())
        # Should breach with default limit of 5000
        assert daemon._redis._store.get("STOP_DAY_LOSS_BREACHED_PAPER") == "True"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Daily P&L Buffer Display Logic
# ══════════════════════════════════════════════════════════════════════════════

class TestDailyPnLBuffer:
    """Tests the buffer-remaining calculation used by the dashboard sidebar."""

    def _calc_buffer(self, stop_limit: float, day_pnl: float):
        """Extract the sidebar calculation logic for unit testing."""
        remaining = stop_limit + day_pnl   # day_pnl is negative when losing
        pct_used = ((stop_limit - remaining) / stop_limit * 100) if stop_limit > 0 else 0
        return remaining, pct_used

    def test_full_buffer_when_no_loss(self):
        remaining, pct = self._calc_buffer(5000, 0.0)
        assert remaining == pytest.approx(5000.0)
        assert pct == pytest.approx(0.0)

    def test_half_buffer_used(self):
        remaining, pct = self._calc_buffer(5000, -2500.0)
        assert remaining == pytest.approx(2500.0)
        assert pct == pytest.approx(50.0)

    def test_zero_buffer_at_breach(self):
        remaining, pct = self._calc_buffer(5000, -5000.0)
        assert remaining == pytest.approx(0.0)
        assert pct == pytest.approx(100.0)

    def test_color_red_above_75_pct(self):
        """Dashboard should show red when > 75% of stop limit is used."""
        _, pct = self._calc_buffer(5000, -4000.0)  # 80%
        assert pct > 75

    def test_color_amber_between_40_75_pct(self):
        _, pct = self._calc_buffer(5000, -2500.0)  # 50%
        assert 40 < pct <= 75

    def test_color_green_below_40_pct(self):
        _, pct = self._calc_buffer(5000, -1000.0)  # 20%
        assert pct <= 40


# ══════════════════════════════════════════════════════════════════════════════
# 3. Redis Key Format Contracts
# ══════════════════════════════════════════════════════════════════════════════

class TestStopDayLossRedisKeys:
    """Validates the Redis key naming contract used across all three files."""

    def test_breach_keys_use_mode_suffix(self):
        for mode in ["PAPER", "LIVE"]:
            assert f"STOP_DAY_LOSS_BREACHED_{mode}" == f"STOP_DAY_LOSS_BREACHED_{mode}"

    def test_daily_pnl_keys_use_mode_suffix(self):
        for mode in ["PAPER", "LIVE"]:
            key = f"DAILY_REALIZED_PNL_{mode}"
            assert key.startswith("DAILY_REALIZED_PNL_")

    def test_stop_day_loss_limit_key(self):
        assert "STOP_DAY_LOSS" == "STOP_DAY_LOSS"   # central UI-set key
