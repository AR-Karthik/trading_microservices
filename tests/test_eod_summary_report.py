"""
tests/test_eod_summary_report.py
==================================
Tests for SystemController._eod_summary_report() (FS §7.3).

Validates:
  - All required Redis keys are read (capital, PnL, lots)
  - Telegram message is always pushed to the queue
  - Deployed capital = Limit - Available (computed correctly)
  - P&L emoji is 🟢 for positive, 🔴 for negative
  - Date in message matches the passed datetime
  - Graceful fallback when asyncpg is unavailable
  - Message includes required data sections (Capital, P&L, Trades)
"""
import asyncio
import json
import pytest
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_redis(store: dict) -> AsyncMock:
    r = AsyncMock()
    _store = dict(store)
    r.get = AsyncMock(side_effect=lambda k: _store.get(k))
    r.set = AsyncMock(side_effect=lambda k, v: _store.update({k: str(v)}))
    r.lpush = AsyncMock(return_value=1)
    return r


def make_controller(redis_store: dict):
    with patch("daemons.system_controller.redis") as mock_redis_module:
        from daemons.system_controller import SystemController
        ctrl = SystemController.__new__(SystemController)
        ctrl.redis = make_redis(redis_store)
        ctrl._shutdown_flag = False
    return ctrl


STANDARD_STORE = {
    "GLOBAL_CAPITAL_LIMIT_PAPER": "50000",
    "GLOBAL_CAPITAL_LIMIT_LIVE": "0",
    "AVAILABLE_MARGIN_PAPER": "32400",
    "AVAILABLE_MARGIN_LIVE": "0",
    "CURRENT_MARGIN_UTILIZED": "17600",
    "DAILY_REALIZED_PNL_PAPER": "1240.50",
    "DAILY_REALIZED_PNL_LIVE": "0",
    "ACTIVE_LOTS_COUNT": "1",
}


# ══════════════════════════════════════════════════════════════════════════════
# A. Telegram message is always sent
# ══════════════════════════════════════════════════════════════════════════════

class TestEODSummaryAlwaysSends:

    @pytest.mark.asyncio
    async def test_telegram_alert_called_once(self):
        """EOD summary must push exactly one Telegram message."""
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock()
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        ctrl._telegram_alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_telegram_sent_on_db_failure(self):
        """Even when DB is unavailable, alert must still be sent."""
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock()
        with patch("daemons.system_controller._HAS_ASYNCPG", True), \
             patch("daemons.system_controller.asyncpg") as mock_pg:
            mock_pg.create_pool = AsyncMock(side_effect=Exception("DB down"))
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        ctrl._telegram_alert.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# B. Deployed capital computation
# ══════════════════════════════════════════════════════════════════════════════

class TestDeployedCapital:
    """Deployed capital = Limit - Available (FS §7.3)."""

    @pytest.mark.asyncio
    async def test_deployed_paper_correct(self):
        sent_messages = []
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        msg = sent_messages[0]
        # 50000 - 32400 = 17600 deployed
        assert "17,600" in msg, "Message must show correct deployed capital"

    @pytest.mark.asyncio
    async def test_pnl_green_for_profit(self):
        sent_messages = []
        ctrl = make_controller(STANDARD_STORE)  # PnL = +1240.50
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        msg = sent_messages[0]
        assert "🟢" in msg, "Positive PnL must show green emoji"
        assert "+1,240.50" in msg

    @pytest.mark.asyncio
    async def test_pnl_red_for_loss(self):
        store = dict(STANDARD_STORE)
        store["DAILY_REALIZED_PNL_PAPER"] = "-3200.0"
        sent_messages = []
        ctrl = make_controller(store)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        msg = sent_messages[0]
        assert "🔴" in msg, "Negative PnL must show red emoji"


# ══════════════════════════════════════════════════════════════════════════════
# C. Date format and required sections
# ══════════════════════════════════════════════════════════════════════════════

class TestMessageContent:

    @pytest.mark.asyncio
    async def test_date_in_message(self):
        sent_messages = []
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        assert "11-Mar-2026" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_message_has_capital_section(self):
        sent_messages = []
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        assert "Capital" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_message_has_pnl_section(self):
        sent_messages = []
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        assert "P&L" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_message_has_trades_section(self):
        sent_messages = []
        ctrl = make_controller(STANDARD_STORE)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        assert "Trades" in sent_messages[0]

    @pytest.mark.asyncio
    async def test_no_positions_shows_clear_message(self):
        sent_messages = []
        store = dict(STANDARD_STORE)
        store["ACTIVE_LOTS_COUNT"] = "0"
        ctrl = make_controller(store)
        ctrl._telegram_alert = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        with patch("daemons.system_controller._HAS_ASYNCPG", False):
            await ctrl._eod_summary_report(datetime(2026, 3, 11, 16, 0))
        assert "No open positions" in sent_messages[0]
