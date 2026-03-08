"""
tests/test_order_reconciler.py
==============================
Tests for the Order Reconciler daemon (SRS §2.5)

Covers:
  - Confirmed order state update (removes from pending_orders)
  - Phantom order detection after 3-second threshold
  - Rejected order cleanup + alert push
  - In-flight order remaining in pending during retries
"""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from daemons.order_reconciler import OrderReconciler, STATUS_COMPLETE, STATUS_REJECTED, STATUS_PENDING


class MockRedis:
    """Minimal async Redis mock."""
    def __init__(self, initial_data=None):
        self._data = initial_data or {}
        self._hash = {}
        self._lists = {}

    async def hgetall(self, key):
        return self._hash.get(key, {})

    async def hset(self, key, field, value=None, mapping=None):
        if key not in self._hash:
            self._hash[key] = {}
        if mapping:
            self._hash[key].update(mapping)
        elif field is not None:
            self._hash[key][field] = value

    async def hdel(self, key, *fields):
        for f in fields:
            self._hash.get(key, {}).pop(f, None)

    async def set(self, key, value):
        self._data[key] = value

    async def get(self, key):
        return self._data.get(key)

    async def incrbyfloat(self, key, amount):
        self._data[key] = float(self._data.get(key, 0)) + amount
        return self._data[key]

    async def expire(self, key, seconds):
        pass

    async def publish(self, channel, message):
        pass

    async def lpush(self, key, *values):
        if key not in self._lists:
            self._lists[key] = []
        self._lists[key].extend(values)

    async def ltrim(self, key, start, end):
        if key in self._lists:
            self._lists[key] = self._lists[key][start:end + 1]

    async def aclose(self):
        pass


class TestOrderReconciler(unittest.IsolatedAsyncioTestCase):

    def _make_reconciler(self, pending_orders=None):
        """Create a reconciler in mock mode."""
        rec = OrderReconciler.__new__(OrderReconciler)
        rec._api = None
        rec._api_available = False
        rec._retry_counts = {}
        rec._dispatch_times = {}
        rec.redis = MockRedis()

        if pending_orders:
            rec.redis._hash["pending_orders"] = {
                oid: json.dumps(meta) for oid, meta in pending_orders.items()
            }
        return rec

    # ── Test 1: Confirmed order path ─────────────────────────────────────────

    async def test_confirmed_order_removed_from_pending(self):
        """A confirmed COMPLETE order should be removed from pending_orders."""
        order_id = "test-order-001"
        meta = {
            "symbol": "NIFTY_ATM_CE",
            "action": "BUY",
            "quantity": 65,
            "strategy_id": "STRAT_GAMMA",
            "execution_type": "Paper",
            "dispatch_time_epoch": time.time()
        }
        rec = self._make_reconciler({order_id: meta})

        # Force 2+ retries so mock returns COMPLETE
        rec._retry_counts[order_id] = 2
        rec._dispatch_times[order_id] = time.time() - 1.0  # 1 second old

        await rec._check_order(order_id, json.dumps(meta))

        # Should be removed from pending
        remaining = rec.redis._hash.get("pending_orders", {})
        self.assertNotIn(order_id, remaining)

    # ── Test 2: Phantom detection ─────────────────────────────────────────────

    async def test_phantom_detected_after_threshold(self):
        """Order older than PHANTOM_THRESHOLD_SEC with no broker ID → phantom."""
        from daemons.order_reconciler import PHANTOM_THRESHOLD_SEC
        order_id = "phantom-order-001"
        meta = {
            "symbol": "BANKNIFTY_ATM_CE",
            "action": "BUY",
            "quantity": 30,
            "strategy_id": "STRAT_REVERSION",
            "execution_type": "Paper",
            "dispatch_time_epoch": time.time() - (PHANTOM_THRESHOLD_SEC + 1)
        }
        rec = self._make_reconciler({order_id: meta})
        rec._dispatch_times[order_id] = meta["dispatch_time_epoch"]
        rec._retry_counts[order_id] = 0

        alerts_before = len(rec.redis._lists.get("telegram_alerts", []))

        await rec._check_order(order_id, json.dumps(meta))

        # Should be removed and Telegram alert pushed
        remaining = rec.redis._hash.get("pending_orders", {})
        self.assertNotIn(order_id, remaining)

        alerts_after = len(rec.redis._lists.get("telegram_alerts", []))
        self.assertGreater(alerts_after, alerts_before, "Expected Telegram alert for phantom order")

    # ── Test 3: Rejected order cleanup ────────────────────────────────────────

    async def test_rejected_order_cleaned_up(self):
        """A REJECTED order should be removed and alert pushed."""
        order_id = "reject-order-001"
        meta = {
            "symbol": "NIFTY_ATM_CE",
            "action": "BUY",
            "quantity": 65,
            "strategy_id": "STRAT_GAMMA",
            "execution_type": "Actual",
            "dispatch_time_epoch": time.time()
        }
        rec = self._make_reconciler({order_id: meta})
        rec._dispatch_times[order_id] = time.time()
        rec._retry_counts[order_id] = 0

        # Patch _query_broker to return REJECTED
        with patch.object(rec, '_query_broker', return_value=(STATUS_REJECTED, 0.0)):
            # Override to be awaitable
            async def mock_query(*args, **kwargs):
                return STATUS_REJECTED, 0.0
            rec._query_broker = mock_query

            await rec._check_order(order_id, json.dumps(meta))

        remaining = rec.redis._hash.get("pending_orders", {})
        self.assertNotIn(order_id, remaining)

    # ── Test 4: In-flight order stays in pending ──────────────────────────────

    async def test_inflight_order_stays_pending(self):
        """A recently dispatched order within threshold should stay in pending."""
        order_id = "inflight-order-001"
        meta = {
            "symbol": "NIFTY_ATM_CE",
            "action": "BUY",
            "quantity": 65,
            "strategy_id": "STRAT_GAMMA",
            "execution_type": "Paper",
            "dispatch_time_epoch": time.time()  # Just dispatched
        }
        rec = self._make_reconciler({order_id: meta})
        rec._dispatch_times[order_id] = time.time()
        rec._retry_counts[order_id] = 0  # First retry → mock returns PENDING

        await rec._check_order(order_id, json.dumps(meta))

        # Should STILL be in pending_orders (not yet timed out)
        remaining = rec.redis._hash.get("pending_orders", {})
        self.assertIn(order_id, remaining)

    # ── Test 5: Position state updated on confirmation ────────────────────────

    async def test_position_state_updated_on_confirm(self):
        """Confirmed order should increment Redis position key."""
        order_id = "confirm-pos-001"
        meta = {
            "symbol": "NIFTY_ATM_CE",
            "action": "BUY",
            "quantity": 65,
            "strategy_id": "STRAT_GAMMA",
            "execution_type": "Paper",
            "dispatch_time_epoch": time.time() - 1.0
        }
        rec = self._make_reconciler({order_id: meta})
        rec._retry_counts[order_id] = 2  # Force COMPLETE

        await rec._mark_order_confirmed(order_id, meta, fill_price=22350.0)

        pos_key = f"position:NIFTY_ATM_CE:STRAT_GAMMA"
        pos_value = rec.redis._data.get(pos_key, 0)
        self.assertEqual(float(pos_value), 65.0)


if __name__ == "__main__":
    unittest.main()
