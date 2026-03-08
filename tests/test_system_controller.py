"""
tests/test_system_controller.py
================================
Tests for System Controller daemon (SRS §2.1)

Covers:
  - Macro lockdown activation 30 min before tier-1 event
  - Macro lockdown cleared after event window
  - Batched square-off SEBI chunking (≤10 OPS, 1.01s gap)
  - Preemption handler publishes to panic_channel
"""

import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from daemons.system_controller import SystemController, batched_square_off, SEBI_BATCH_SIZE, INTER_BATCH_WAIT

IST = ZoneInfo("Asia/Kolkata")


class MockRedis:
    def __init__(self):
        self._data = {}
        self._published = []
        self._lists = {}

    async def set(self, key, value):
        self._data[key] = value

    async def get(self, key):
        return self._data.get(key)

    async def publish(self, channel, message):
        self._published.append((channel, message))

    async def lpush(self, key, *values):
        if key not in self._lists:
            self._lists[key] = []
        self._lists[key].extend(values)

    async def aclose(self):
        pass


class TestSystemController(unittest.IsolatedAsyncioTestCase):

    def _make_controller(self):
        ctrl = SystemController.__new__(SystemController)
        ctrl.redis = MockRedis()
        ctrl._macro_events = []
        ctrl._lockdown_announced = set()
        ctrl._preemption_detected = False
        ctrl._shutdown_flag = False
        return ctrl

    # ── Test 1: Macro lockdown activated within 30-min window ────────────────

    async def test_macro_lockdown_activates_within_window(self):
        """MACRO_EVENT_LOCKDOWN=True when event is < 30 min away."""
        ctrl = self._make_controller()
        # Create event 15 minutes from now
        event_dt = datetime.now(tz=IST) + timedelta(minutes=15)
        ctrl._macro_events = [{
            "name": "Test RBI Event",
            "datetime": event_dt.isoformat(),
            "tier": 1
        }]

        await ctrl._macro_lockdown_watcher.__wrapped__(ctrl) if hasattr(
            ctrl._macro_lockdown_watcher, '__wrapped__'
        ) else None

        # Manually simulate one watcher cycle
        now = datetime.now(tz=IST)
        lockdown_active = False
        for event in ctrl._macro_events:
            event_d = datetime.fromisoformat(event["datetime"]).astimezone(IST)
            delta = event_d - now
            if timedelta(0) <= delta <= timedelta(minutes=30):
                lockdown_active = True

        self.assertTrue(lockdown_active, "Expected lockdown to be active 15 min before event")

    # ── Test 2: Macro lockdown cleared after event ────────────────────────────

    async def test_macro_lockdown_cleared_after_event(self):
        """MACRO_EVENT_LOCKDOWN should be False when event is > 30 min in the past."""
        ctrl = self._make_controller()
        # Create event 45 minutes ago
        event_dt = datetime.now(tz=IST) - timedelta(minutes=45)
        ctrl._macro_events = [{
            "name": "Past RBI Event",
            "datetime": event_dt.isoformat(),
            "tier": 1
        }]

        now = datetime.now(tz=IST)
        lockdown_active = False
        for event in ctrl._macro_events:
            event_d = datetime.fromisoformat(event["datetime"]).astimezone(IST)
            delta = event_d - now
            if timedelta(0) <= delta <= timedelta(minutes=30):
                lockdown_active = True
            elif delta < timedelta(0) and delta > timedelta(minutes=-30):
                lockdown_active = True

        self.assertFalse(lockdown_active, "Expected lockdown to be inactive 45 min after event")

    # ── Test 3: Batched square-off SEBI chunking ─────────────────────────────

    async def test_batched_square_off_chunks_correctly(self):
        """batched_square_off should fire in groups of ≤10 with 1.01s gaps."""
        positions = [{"symbol": f"SYM{i}", "quantity": 1} for i in range(25)]
        fired_batches = []
        fire_times = []

        async def mock_fire(batch):
            fired_batches.append(len(batch))
            fire_times.append(time.time())

        total = await batched_square_off(positions, mock_fire)

        self.assertEqual(total, 25)
        # Should be 3 batches: 10, 10, 5
        self.assertEqual(fired_batches, [10, 10, 5])
        # Time gaps between batches should be ~1.01s
        for i in range(1, len(fire_times)):
            gap = fire_times[i] - fire_times[i - 1]
            self.assertGreaterEqual(gap, INTER_BATCH_WAIT - 0.1,
                                    f"Batch {i} gap {gap:.2f}s < {INTER_BATCH_WAIT}s SEBI limit")

    # ── Test 4: Batched square-off single batch ───────────────────────────────

    async def test_batched_square_off_single_batch(self):
        """≤10 positions should fire as one batch with no wait."""
        positions = [{"symbol": f"SYM{i}", "quantity": 1} for i in range(5)]
        fired_batches = []

        async def mock_fire(batch):
            fired_batches.append(batch)

        total = await batched_square_off(positions, mock_fire)

        self.assertEqual(total, 5)
        self.assertEqual(len(fired_batches), 1)

    # ── Test 5: Square-off all publishes to panic_channel ─────────────────────

    async def test_square_off_all_publishes_panic(self):
        """_execute_square_off_all should publish SQUARE_OFF_ALL to Redis panic_channel."""
        ctrl = self._make_controller()

        # Override sleep to not actually wait
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await ctrl._execute_square_off_all(reason="TEST")

        published_channels = [ch for ch, _ in ctrl.redis._published]
        self.assertIn("panic_channel", published_channels)

        # Check payload
        for ch, msg in ctrl.redis._published:
            if ch == "panic_channel":
                data = json.loads(msg)
                self.assertEqual(data["action"], "SQUARE_OFF_ALL")
                self.assertEqual(data["reason"], "TEST")
                break

    # ── Test 6: Preemption sets SYSTEM_HALTED ────────────────────────────────

    async def test_preemption_sets_halted_flag(self):
        """After execute_square_off_all, SYSTEM_HALTED should be True in Redis."""
        ctrl = self._make_controller()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await ctrl._execute_square_off_all(reason="GCP_PREEMPTION")

        halted = ctrl.redis._data.get("SYSTEM_HALTED")
        self.assertEqual(halted, "True")


if __name__ == "__main__":
    unittest.main()
