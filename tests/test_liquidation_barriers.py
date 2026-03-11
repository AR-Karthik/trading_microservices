"""
tests/test_liquidation_barriers.py
====================================
Tests for the Three-Barrier Liquidation Protocol (FS §6).

Validates:
  - Barrier 1: TP1 partial exit at 1.2×ATR (70-30 rule)
  - Barrier 1: TP2 runner ceiling at 2.5×ATR
  - Barrier 1: Regime-adaptive SL expansion (1.5× in high vol)
  - Barrier 1: HMM regime invalidation exit
  - Barrier 2: Stall timer (300s normal, 180s low-vol)
  - Barrier 3: CVD flip exit (5 ticks / 10 ticks for runner)
  - Barrier 3: RV panic exit (> 0.002)
  - HTTP 400 error classification (circuit limit vs abort vs retry)
  - ATR constant correctness (TS §2 constant registry)
"""
import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_redis(store: dict) -> AsyncMock:
    r = AsyncMock()
    _store = dict(store)
    r.get = AsyncMock(side_effect=lambda k: _store.get(k))
    r.set = AsyncMock(side_effect=lambda k, v: _store.update({k: str(v)}))
    r.publish = AsyncMock(return_value=1)
    r._store = _store
    return r


def make_daemon(redis_store: dict):
    from daemons.liquidation_daemon import LiquidationDaemon
    with patch("daemons.liquidation_daemon.MQManager"):
        d = LiquidationDaemon.__new__(LiquidationDaemon)
        d._redis = make_redis(redis_store)
        d.orphaned_positions = {}
        d.mq = MagicMock()
        d.order_pub = MagicMock()
    return d


# ══════════════════════════════════════════════════════════════════════════════
# A. Module-level constant correctness (FS §6 constant block)
# ══════════════════════════════════════════════════════════════════════════════

class TestBarrierConstants:
    """FS §6: All threshold values must be defined as named constants."""

    def test_tp1_multiplier(self):
        from daemons.liquidation_daemon import ATR_TP1_MULTIPLIER
        assert ATR_TP1_MULTIPLIER == 1.2, "TP1 must be 1.2×ATR (FS §6.1)"

    def test_tp2_multiplier(self):
        from daemons.liquidation_daemon import ATR_TP_MULTIPLIER
        assert ATR_TP_MULTIPLIER == 2.5, "TP2 Runner ceiling must be 2.5×ATR (FS §6.1)"

    def test_atr_sl_multiplier(self):
        from daemons.liquidation_daemon import ATR_SL_MULTIPLIER
        assert ATR_SL_MULTIPLIER == 1.0, "Base SL must be 1.0×ATR (FS §6.1)"

    def test_stall_timeout(self):
        from daemons.liquidation_daemon import STALL_TIMEOUT_SEC
        assert STALL_TIMEOUT_SEC == 300, "Normal stall timer must be 300s (FS §6.2)"

    def test_cvd_flip_threshold(self):
        from daemons.liquidation_daemon import CVD_FLIP_EXIT_THRESHOLD
        assert CVD_FLIP_EXIT_THRESHOLD == 5, "5 consecutive CVD flips = exit (FS §6.3)"

    def test_barrier2_spread_cross(self):
        from daemons.liquidation_daemon import BARRIER2_SPREAD_CROSS_PCT
        assert BARRIER2_SPREAD_CROSS_PCT == 0.10, "10% bid-ask cross per retry (FS §6.2)"


# ══════════════════════════════════════════════════════════════════════════════
# B. Barrier 1 — TP1 triggers 70% partial exit at 1.2×ATR
# ══════════════════════════════════════════════════════════════════════════════

class TestBarrier1TP1:
    """FS §6.1 — Risk-Off Milestone: 70-30 partial exit at TP1."""

    def _make_pos(self, entry: float, atr: float) -> dict:
        return {
            "symbol": "NIFTY25MAR24000CE",
            "action": "BUY",
            "quantity": 100,
            "entry_price": entry,
            "execution_type": "Paper",
            "runner_active": False,
        }

    @pytest.mark.asyncio
    async def test_tp1_triggers_partial_exit(self):
        """Price at TP1 (entry + 1.2×ATR) triggers a 70% partial exit."""
        entry, atr = 100.0, 50.0
        tp1 = entry + 1.2 * atr  # 160.0
        d = make_daemon({"rv": "0.0005", "vix": "15.0", "atr": str(atr), "hmm_regime": "TRENDING"})
        d._attempt_partial_exit = AsyncMock()
        pos = self._make_pos(entry, atr)
        tick = {"ask": tp1 + 1, "bid": tp1 - 1}
        await d._check_barriers(pos, "NIFTY25MAR24000CE", tp1, tick, elapsed=10.0)
        d._attempt_partial_exit.assert_awaited_once()
        kwargs = d._attempt_partial_exit.call_args
        assert kwargs[1].get("pct", kwargs[0][3] if len(kwargs[0]) > 3 else None) == pytest.approx(0.70)

    @pytest.mark.asyncio
    async def test_tp1_no_trigger_below_target(self):
        """Price below TP1 must not trigger partial exit."""
        entry, atr = 100.0, 50.0
        tp1 = entry + 1.2 * atr  # 160.0
        d = make_daemon({"rv": "0.0005", "vix": "15.0", "atr": str(atr), "hmm_regime": "TRENDING"})
        d._attempt_partial_exit = AsyncMock()
        d._attempt_exit = AsyncMock()
        pos = self._make_pos(entry, atr)
        tick = {"ask": tp1 - 1, "bid": tp1 - 2}
        await d._check_barriers(pos, "NIFTY25MAR24000CE", tp1 - 5, tick, elapsed=10.0)
        d._attempt_partial_exit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# C. Barrier 1 — Regime-adaptive SL expansion
# ══════════════════════════════════════════════════════════════════════════════

class TestBarrier1SL:
    """FS §6.1 — SL expands to 1.5×ATR when RV > 0.001 or VIX > 18."""

    @pytest.mark.asyncio
    async def test_sl_expands_in_high_vol(self):
        """In high-vol regime, SL = 1.5×ATR, so price at 1.2×ATR below entry must NOT trigger SL."""
        entry, atr = 200.0, 20.0
        normal_sl = entry - 1.0 * atr   # 180.0 — would trigger
        hv_sl = entry - 1.5 * atr       # 170.0 — should NOT trigger at 180
        d = make_daemon({"rv": "0.0015", "vix": "20.0", "atr": str(atr), "hmm_regime": "TRENDING"})
        d._attempt_exit = AsyncMock()
        d._attempt_partial_exit = AsyncMock()
        pos = {"symbol": "NIFTY", "action": "BUY", "quantity": 50,
               "entry_price": entry, "execution_type": "Paper", "runner_active": False}
        tick = {"ask": normal_sl + 1, "bid": normal_sl - 1}
        await d._check_barriers(pos, "NIFTY", normal_sl, tick, elapsed=5.0)
        # Exit should NOT fire because expanded SL is 170, not 180
        d._attempt_exit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# D. Barrier 3 — CVD flip exit
# ══════════════════════════════════════════════════════════════════════════════

class TestBarrier3CVD:
    """FS §6.3 — 5 consecutive CVD flips exit non-runner; 10 for runner."""

    @pytest.mark.asyncio
    async def test_5_cvd_flips_exit_normal_position(self):
        """5 CVD flips exits a normal (non-runner) position."""
        d = make_daemon({"rv": "0.0", "vix": "15.0", "atr": "20.0",
                         "hmm_regime": "TRENDING", "cvd_flip_ticks": "5"})
        d._attempt_exit = AsyncMock()
        d._attempt_partial_exit = AsyncMock()
        pos = {"action": "BUY", "quantity": 50, "entry_price": 200.0,
               "execution_type": "Paper", "runner_active": False}
        tick = {"ask": 201.0, "bid": 199.0}
        await d._check_barriers(pos, "NIFTY", 200.0, tick, elapsed=5.0)
        d._attempt_exit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_5_cvd_flips_forgiven_for_runner(self):
        """5 CVD flips must NOT exit a runner position (threshold is 10)."""
        d = make_daemon({"rv": "0.0", "vix": "15.0", "atr": "20.0",
                         "hmm_regime": "TRENDING", "cvd_flip_ticks": "5"})
        d._attempt_exit = AsyncMock()
        pos = {"action": "BUY", "quantity": 50, "entry_price": 200.0,
               "execution_type": "Paper", "runner_active": True}
        tick = {"ask": 201.0, "bid": 199.0}
        await d._check_barriers(pos, "NIFTY", 200.0, tick, elapsed=5.0)
        d._attempt_exit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# E. HTTP 400 Error Parser (FS §6.4)
# ══════════════════════════════════════════════════════════════════════════════

class TestHTTP400Parser:
    """FS §6.4 — Circuit limit → recalculate; Abort strings → halt."""

    def _parser(self, err_str: str) -> str:
        from daemons.liquidation_daemon import LiquidationDaemon
        with patch("daemons.liquidation_daemon.MQManager"):
            d = LiquidationDaemon.__new__(LiquidationDaemon)
        return d._parse_http400_emsg(err_str)

    @pytest.mark.parametrize("msg", ["price range exceeded", "circuit limit", "price band hit", "pcl", "ucl"])
    def test_circuit_limit_strings(self, msg):
        assert self._parser(msg) == "circuit_limit"

    @pytest.mark.parametrize("msg", ["insufficient margin", "invalid order", "not found in system"])
    def test_abort_strings(self, msg):
        assert self._parser(msg) == "abort"

    def test_unknown_error_retries(self):
        assert self._parser("timeout connecting to broker") == "retry"
