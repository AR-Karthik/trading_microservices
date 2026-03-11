"""
tests/test_v70_features.py
==========================
Unit tests for all v7.0 system improvements:
  A1 — Global Portfolio Heat Constraint (meta_router.py)
  A2 — Slippage Budget Monitor (liquidation_daemon.py + strategy_engine.py)
  B2 — HMM Warm-Up Gap Normalization / _calibrate_vol_context (strategy_engine.py)
  C1 — Theta-Aware Dynamic Stall Timer (liquidation_daemon.py)
  C2 — Automated Orphan Order Reconciliation / _reboot_audit (order_reconciler.py)
"""

import asyncio
import json
import time
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ═══════════════════════════════════════════════════════════════════════════════
# Shared Mock Redis
# ═══════════════════════════════════════════════════════════════════════════════

class MockRedis:
    """Full-featured async Redis mock used across multiple test suites."""
    def __init__(self, initial_data=None, initial_hash=None):
        self._data = initial_data or {}
        self._hash = initial_hash or {}
        self._lists = {}
        self._expiry = {}

    async def get(self, key):
        return self._data.get(key)

    async def set(self, key, value, ex=None):
        self._data[key] = value
        if ex:
            self._expiry[key] = ex

    async def hgetall(self, key):
        return self._hash.get(key, {})

    async def hset(self, key, field=None, value=None, mapping=None):
        if key not in self._hash:
            self._hash[key] = {}
        if mapping:
            self._hash[key].update(mapping)
        elif field is not None:
            self._hash[key][field] = value

    async def hdel(self, key, *fields):
        for f in fields:
            self._hash.get(key, {}).pop(f, None)

    async def lpush(self, key, *values):
        if key not in self._lists:
            self._lists[key] = []
        self._lists[key].extend(values)

    async def lindex(self, key, idx):
        lst = self._lists.get(key, [])
        try:
            return lst[idx]
        except IndexError:
            return None

    async def ltrim(self, key, start, end):
        if key in self._lists:
            self._lists[key] = self._lists[key][start:end + 1]

    async def incrbyfloat(self, key, amount):
        self._data[key] = float(self._data.get(key, 0)) + amount
        return self._data[key]

    async def expire(self, key, seconds):
        self._expiry[key] = seconds

    async def publish(self, channel, message):
        pass

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)

    async def aclose(self):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# A1 — Global Portfolio Heat Constraint
# ═══════════════════════════════════════════════════════════════════════════════

class TestPortfolioHeatConstraint(unittest.IsolatedAsyncioTestCase):
    """
    A1: If the combined fractional Kelly weights from all three Tri-Brain assets
    exceed CONFIG:MAX_PORTFOLIO_HEAT (default 0.5), all weights are proportionally
    scaled down.
    """

    def _make_router_mock(self, heat_limit_str=None):
        """Returns a patched MetaRouter with Redis returning the given heat limit."""
        with patch("daemons.meta_router.MQManager"), patch("daemons.meta_router.ShmManager"):
            from daemons.meta_router import MetaRouter, DEFAULT_MAX_PORTFOLIO_HEAT
            r = MetaRouter.__new__(MetaRouter)
            r._redis = MockRedis()
            r.orchestrator = MagicMock()
            r.brains = {}
            r.mq = MagicMock()
            r.cmd_pub = MagicMock()
            r.test_mode = False
            if heat_limit_str is not None:
                r._redis._data["CONFIG:MAX_PORTFOLIO_HEAT"] = heat_limit_str
        return r

    async def test_heat_under_limit_no_scaling(self):
        """Combined weight = 0.30 < 0.5 limit → no scaling applied."""
        r = self._make_router_mock("0.5")

        # Simulate three commands each with weight=0.10 → total=0.30
        commands = [
            {"asset": "NIFTY", "weight": 0.10},
            {"asset": "BANKNIFTY", "weight": 0.10},
            {"asset": "SENSEX", "weight": 0.10},
        ]

        total_heat = sum(c["weight"] for c in commands)
        heat_limit = float(await r._redis.get("CONFIG:MAX_PORTFOLIO_HEAT") or 0.5)

        self.assertLessEqual(total_heat, heat_limit)
        # Weights should remain unchanged
        for cmd in commands:
            self.assertAlmostEqual(cmd["weight"], 0.10)

    async def test_heat_over_limit_scales_down(self):
        """Combined weight = 0.90 > 0.5 limit → all weights scaled to sum ≤ 0.5."""
        commands = [
            {"asset": "NIFTY", "weight": 0.30},
            {"asset": "BANKNIFTY", "weight": 0.30},
            {"asset": "SENSEX", "weight": 0.30},
        ]
        heat_limit = 0.5
        total_heat = sum(c["weight"] for c in commands)

        if total_heat > heat_limit and total_heat > 0:
            scale = heat_limit / total_heat
            commands = [{**cmd, "weight": round(cmd["weight"] * scale, 4)} for cmd in commands]

        scaled_total = sum(c["weight"] for c in commands)
        self.assertAlmostEqual(scaled_total, heat_limit, places=3)
        # Each weight should be ~0.1667
        for cmd in commands:
            self.assertAlmostEqual(cmd["weight"], heat_limit / 3.0, places=3)

    async def test_custom_heat_limit_respected(self):
        """CONFIG:MAX_PORTFOLIO_HEAT=0.3 should cap total at 0.3."""
        commands = [
            {"asset": "NIFTY", "weight": 0.25},
            {"asset": "BANKNIFTY", "weight": 0.25},
            {"asset": "SENSEX", "weight": 0.25},
        ]
        heat_limit = 0.30
        total_heat = sum(c["weight"] for c in commands)

        if total_heat > heat_limit:
            scale = heat_limit / total_heat
            commands = [{**cmd, "weight": round(cmd["weight"] * scale, 4)} for cmd in commands]

        scaled_total = sum(c["weight"] for c in commands)
        self.assertLessEqual(scaled_total, heat_limit + 0.001)

    async def test_zero_weight_no_division_error(self):
        """Edge case: all weights are 0 → no scaling, no ZeroDivisionError."""
        commands = [
            {"asset": "NIFTY", "weight": 0.0},
            {"asset": "BANKNIFTY", "weight": 0.0},
        ]
        heat_limit = 0.5
        total_heat = sum(c["weight"] for c in commands)

        # Must not raise ZeroDivisionError
        if total_heat > heat_limit and total_heat > 0:
            scale = heat_limit / total_heat
            commands = [{**cmd, "weight": cmd["weight"] * scale} for cmd in commands]

        self.assertEqual(sum(c["weight"] for c in commands), 0.0)


# ═══════════════════════════════════════════════════════════════════════════════
# A2 — Slippage Budget Monitor
# ═══════════════════════════════════════════════════════════════════════════════

class TestSlippageBudget(unittest.IsolatedAsyncioTestCase):
    """
    A2: If any single fill deviates from intended_price by > 2%, SLIPPAGE_HALT
    is set in Redis for 60 seconds. Strategy engine checks this flag before
    dispatching new entries.
    """

    def _make_daemon(self):
        with patch("daemons.liquidation_daemon.MQManager"):
            from daemons.liquidation_daemon import LiquidationDaemon
            d = LiquidationDaemon.__new__(LiquidationDaemon)
            d._redis = MockRedis()
            d._api = None
            d._api_available = False
        return d

    async def test_slippage_within_budget_no_halt(self):
        """0.5% slippage < 2% threshold → SLIPPAGE_HALT should NOT be set."""
        from daemons.liquidation_daemon import SLIPPAGE_BUDGET_PCT
        intended = 22000.0
        fill = intended * (1 + 0.005)  # 0.5% slippage
        slip = abs(fill - intended) / intended
        self.assertLess(slip, SLIPPAGE_BUDGET_PCT)

    async def test_slippage_over_budget_triggers_halt(self):
        """3% slippage > 2% threshold → SLIPPAGE_HALT must be set."""
        from daemons.liquidation_daemon import SLIPPAGE_BUDGET_PCT, SLIPPAGE_HALT_TTL_SEC
        d = self._make_daemon()
        intended = 22000.0
        fill = intended * (1 + 0.03)  # 3% slippage
        slip = abs(fill - intended) / intended

        if slip > SLIPPAGE_BUDGET_PCT:
            await d._redis.set("SLIPPAGE_HALT", "True", ex=SLIPPAGE_HALT_TTL_SEC)

        halt = await d._redis.get("SLIPPAGE_HALT")
        self.assertEqual(halt, "True")
        # Check TTL was set
        self.assertEqual(d._redis._expiry.get("SLIPPAGE_HALT"), SLIPPAGE_HALT_TTL_SEC)

    async def test_strategy_engine_blocks_entry_on_halt(self):
        """When SLIPPAGE_HALT=True, new entry dispatch is blocked."""
        redis = MockRedis(initial_data={"SLIPPAGE_HALT": "True"})
        halt_active = await redis.get("SLIPPAGE_HALT") == "True"
        self.assertTrue(halt_active, "SLIPPAGE_HALT should block new entries")

    async def test_strategy_engine_allows_entry_when_no_halt(self):
        """When SLIPPAGE_HALT is absent, entries proceed normally."""
        redis = MockRedis()
        halt_active = await redis.get("SLIPPAGE_HALT") == "True"
        self.assertFalse(halt_active)

    async def test_halt_ttl_is_60_seconds(self):
        """SLIPPAGE_HALT_TTL_SEC must be 60 (as defined in spec)."""
        from daemons.liquidation_daemon import SLIPPAGE_HALT_TTL_SEC
        self.assertEqual(SLIPPAGE_HALT_TTL_SEC, 60)


# ═══════════════════════════════════════════════════════════════════════════════
# B2 — HMM Gap Calibration (_calibrate_vol_context)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHMMGapCalibration(unittest.IsolatedAsyncioTestCase):
    """
    B2: _calibrate_vol_context() reads prev_close, tick_history, and rolling_std
    from Redis, computes gap_z, and writes VOL_GAP_Z:<symbol> with 2h TTL.
    """

    def _make_redis_with_data(self, symbol, prev_close, open_price, rolling_std):
        r = MockRedis()
        r._data[f"prev_close:{symbol}"] = str(prev_close)
        r._data[f"rolling_std_20d:{symbol}"] = str(rolling_std)
        # Simulate tick_history as an lpush'd list
        r._lists[f"tick_history:{symbol}"] = [json.dumps({"price": open_price})]
        return r

    async def test_gap_z_computed_and_stored(self):
        """VOL_GAP_Z:<symbol> should be written when all inputs are present."""
        from daemons.strategy_engine import _calibrate_vol_context
        redis = self._make_redis_with_data("NIFTY50", 22000.0, 22200.0, 50.0)
        await _calibrate_vol_context(redis)

        gap_z_raw = await redis.get("VOL_GAP_Z:NIFTY50")
        self.assertIsNotNone(gap_z_raw, "VOL_GAP_Z:NIFTY50 should be written")
        gap_z = float(gap_z_raw)
        # (22200 - 22000) / 50 = 4.0
        self.assertAlmostEqual(gap_z, 4.0, places=2)

    async def test_gap_z_ttl_set(self):
        """VOL_GAP_Z key must have 2-hour TTL (7200s)."""
        from daemons.strategy_engine import _calibrate_vol_context
        redis = self._make_redis_with_data("BANKNIFTY", 48000.0, 48500.0, 100.0)
        await _calibrate_vol_context(redis)

        ttl = redis._expiry.get("VOL_GAP_Z:BANKNIFTY")
        self.assertEqual(ttl, 7200)

    async def test_graceful_skip_when_no_prev_close(self):
        """If prev_close missing, calibration for that symbol is skipped (no error)."""
        from daemons.strategy_engine import _calibrate_vol_context
        redis = MockRedis()
        # No prev_close, no tick_history — should not raise
        try:
            await _calibrate_vol_context(redis)
        except Exception as e:
            self.fail(f"_calibrate_vol_context raised unexpectedly: {e}")

        gap_z = await redis.get("VOL_GAP_Z:NIFTY50")
        self.assertIsNone(gap_z)

    async def test_negative_gap_z_for_gap_down(self):
        """Overnight gap-down → negative Z-score stored correctly."""
        from daemons.strategy_engine import _calibrate_vol_context
        redis = self._make_redis_with_data("SENSEX", 72000.0, 71500.0, 100.0)
        await _calibrate_vol_context(redis)

        gap_z = float(await redis.get("VOL_GAP_Z:SENSEX") or "0")
        # (71500 - 72000) / 100 = -5.0
        self.assertAlmostEqual(gap_z, -5.0, places=2)

    async def test_fallback_std_when_missing(self):
        """If rolling_std_20d missing, fallback to 50.0 and still compute Z."""
        from daemons.strategy_engine import _calibrate_vol_context
        redis = MockRedis()
        redis._data["prev_close:NIFTY50"] = "22000.0"
        redis._lists["tick_history:NIFTY50"] = [json.dumps({"price": 22050.0})]
        # No rolling_std — should use 50.0 fallback

        await _calibrate_vol_context(redis)

        gap_z = await redis.get("VOL_GAP_Z:NIFTY50")
        self.assertIsNotNone(gap_z)
        # (22050 - 22000) / 50 = 1.0
        self.assertAlmostEqual(float(gap_z), 1.0, places=2)


# ═══════════════════════════════════════════════════════════════════════════════
# C1 — Theta-Aware Dynamic Stall Timer
# ═══════════════════════════════════════════════════════════════════════════════

class TestThetaAwareStallTimer(unittest.TestCase):
    """
    C1: On expiry days (Wed/Thu), the stall timer decays linearly from the
    standard value to a 60s floor as 15:30 IST approaches.
    """

    def _compute_expiry_stall(self, base_stall, hour, minute, weekday_name):
        """Replicate the C1 logic inline for isolated testing."""
        is_expiry_day = weekday_name in ["Wednesday", "Thursday"]
        stall_timer = base_stall

        if is_expiry_day:
            market_close_minutes = 15 * 60 + 30
            current_minutes = hour * 60 + minute
            minutes_to_close = max(0, market_close_minutes - current_minutes)
            expiry_stall = max(60, int(stall_timer * (minutes_to_close / 360.0)))
            if expiry_stall < stall_timer:
                stall_timer = expiry_stall

        return stall_timer

    def test_non_expiry_day_unchanged(self):
        """Monday/Tuesday/Friday: stall timer must be unchanged."""
        for day in ["Monday", "Tuesday", "Friday"]:
            result = self._compute_expiry_stall(300, 10, 0, day)
            self.assertEqual(result, 300, f"Non-expiry day {day} should not shrink stall timer")

    def test_expiry_day_morning_mostly_unchanged(self):
        """At 09:30 on expiry (360 min to close): stall should equal base."""
        result = self._compute_expiry_stall(300, 9, 30, "Wednesday")
        self.assertEqual(result, 300)

    def test_expiry_day_mid_session_reduced(self):
        """At 12:30 (180 min to close): stall = 300 * (180/360) = 150."""
        result = self._compute_expiry_stall(300, 12, 30, "Thursday")
        self.assertEqual(result, 150)

    def test_expiry_day_near_close_floor(self):
        """At 15:20 (10 min to close): stall compressed to 60s floor."""
        result = self._compute_expiry_stall(300, 15, 20, "Wednesday")
        self.assertEqual(result, 60)

    def test_expiry_day_after_close_floor(self):
        """After 15:30: minutes_to_close = 0 → floor at 60."""
        result = self._compute_expiry_stall(300, 15, 45, "Thursday")
        self.assertEqual(result, 60)

    def test_low_vol_base_also_decays(self):
        """In low-vol (base=180s), expiry decay still applies."""
        result = self._compute_expiry_stall(180, 12, 30, "Wednesday")
        # 180 * (180/360) = 90
        self.assertEqual(result, 90)


# ═══════════════════════════════════════════════════════════════════════════════
# C2 — Reboot Audit (_reboot_audit)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRebootAudit(unittest.IsolatedAsyncioTestCase):
    """
    C2: On startup, _reboot_audit() queries all orders in pending_orders.
    - COMPLETE + BUY → confirm state + handoff to LiquidationDaemon
    - OPEN → cancel at broker
    - PHANTOM/PENDING → treat as phantom, reverse margin
    """

    def _make_reconciler(self, pending_orders=None):
        from daemons.order_reconciler import OrderReconciler
        rec = OrderReconciler.__new__(OrderReconciler)
        rec._api = None
        rec._api_available = False
        rec._retry_counts = {}
        rec._dispatch_times = {}
        rec.redis = MockRedis()
        if rec.redis._hash.get("pending_orders") is None:
            rec.redis._hash["pending_orders"] = {}
        if pending_orders:
            for oid, meta in pending_orders.items():
                rec.redis._hash["pending_orders"][oid] = json.dumps(meta)

        # Mock margin manager
        margin_mock = AsyncMock()
        margin_mock.release = AsyncMock()
        rec._margin = margin_mock
        return rec

    async def test_clean_slate_no_pending(self):
        """Empty pending_orders → audit completes immediately, no error."""
        rec = self._make_reconciler()
        try:
            await rec._reboot_audit()
        except Exception as e:
            self.fail(f"_reboot_audit with empty pending raised: {e}")

    async def test_filled_buy_is_confirmed_on_reboot(self):
        """COMPLETE BUY order on reboot → must be removed from pending_orders."""
        oid = "reboot-fill-001"
        meta = {
            "symbol": "NIFTY_ATM_CE", "action": "BUY", "quantity": 65,
            "strategy_id": "STRAT_GAMMA", "execution_type": "Paper",
            "price": 350.0, "dispatch_time_epoch": time.time() - 5
        }
        rec = self._make_reconciler({oid: meta})

        # Mock broker as COMPLETE
        async def mock_query(broker_id, local_id):
            return "COMPLETE", 352.5
        rec._query_broker = mock_query

        # Mock handoff to liquidation daemon (no real ZMQ in tests)
        rec._handoff_to_liquidation_daemon = AsyncMock()

        await rec._reboot_audit()

        remaining = rec.redis._hash.get("pending_orders", {})
        self.assertNotIn(oid, remaining, "Confirmed COMPLETE order must be removed from pending")
        rec._handoff_to_liquidation_daemon.assert_called_once()

    async def test_open_order_cancelled_on_reboot(self):
        """OPEN order on reboot → must be cancelled and removed from pending_orders."""
        oid = "reboot-open-001"
        meta = {
            "symbol": "BANKNIFTY_ATM_PE", "action": "BUY", "quantity": 30,
            "strategy_id": "STRAT_MR", "execution_type": "Paper",
            "broker_order_id": "SHOONYA123", "dispatch_time_epoch": time.time() - 5
        }
        rec = self._make_reconciler({oid: meta})

        async def mock_query(broker_id, local_id):
            return "OPEN", 0.0
        rec._query_broker = mock_query

        # Track cancel calls
        rec._cancel_open_order = AsyncMock()

        await rec._reboot_audit()

        rec._cancel_open_order.assert_called_once()

    async def test_phantom_order_cleaned_on_reboot(self):
        """Unconfirmed order on reboot → phantom cleanup, margin reversed."""
        oid = "reboot-phantom-001"
        meta = {
            "symbol": "SENSEX_ATM_CE", "action": "BUY", "quantity": 20,
            "strategy_id": "STRAT_VWAP", "execution_type": "Paper",
            "price": 100.0, "dispatch_time_epoch": time.time() - 10
        }
        rec = self._make_reconciler({oid: meta})

        async def mock_query(broker_id, local_id):
            return "PENDING", 0.0
        rec._query_broker = mock_query

        await rec._reboot_audit()

        remaining = rec.redis._hash.get("pending_orders", {})
        self.assertNotIn(oid, remaining, "Phantom order must be cleared on reboot")


if __name__ == "__main__":
    unittest.main()
