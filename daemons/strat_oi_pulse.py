"""
daemons/strat_oi_pulse.py
==========================
Strategy 4: OI Pulse Scalping (SRS §3.4)

Regime: Any (fires on OI acceleration burst regardless of regime)
Entry:
  - ATM±100pt OI acceleration > 300% of 10-min rolling OI average
  - Spot trending toward the strike with highest OI acceleration
  - DTE ≤ 3
Invalidation:
  - Spot crosses and stays beyond the OI wall for > 30 seconds
  - (Suggests market is pushing through, not respecting the wall)
"""

import asyncio
import collections
import json
import logging
import math
import sys
import time
import uuid
from datetime import datetime, timezone

import redis

from core.mq import MQManager, Ports, Topics
from core.margin import MarginManager

logger = logging.getLogger("StratOIPulse")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    stream=sys.stdout)

OI_ACCEL_THRESHOLD = 3.0    # 300% of rolling average
OI_WINDOW_MIN = 10           # minutes for rolling OI baseline
OI_WINDOW_TICKS = 600        # ~10 min at 1 tick/sec
ATM_RANGE_PTS = 100          # Look at strikes ±100 from spot
BREACH_TIMEOUT_SEC = 30      # 30s beyond OI wall → invalidation
DTE_MAX = 3


class OIPulseStrategy:
    """Buy-only OI acceleration burst scalper."""

    STRATEGY_ID = "STRAT_OI_PULSE"

    def __init__(self, redis_client: redis.Redis, mq: MQManager):
        self._redis = redis_client
        self._mq = mq
        self._push = mq.create_push(Ports.ORDERS, bind=False)
        self._margin = MarginManager(self._redis)
        self._active = False
        self._position = 0
        self._entry_price = 0.0
        self._entry_time = 0.0

        # OI history: {strike: deque of (timestamp, oi) pairs}
        self._oi_history: dict[int, collections.deque] = {}
        # Price history for trending check
        self._price_history: collections.deque = collections.deque(maxlen=100)
        # OI wall breach tracker: {strike: first_breach_timestamp}
        self._wall_breach_times: dict[int, float] = {}

    # ── Command Handler ───────────────────────────────────────────────────────

    async def handle_command(self, cmd: dict):
        command = cmd.get("command", "")
        if command == "ACTIVATE":
            if not self._active:
                logger.info("STRAT_OI_PULSE: ACTIVATED")
            self._active = True
        elif command in ("PAUSE", "ORPHAN"):
            if self._active and self._position > 0:
                await self._issue_orphan()
            self._active = False
            logger.info(f"STRAT_OI_PULSE: {command}")

    # ── OI Ingestion ──────────────────────────────────────────────────────────

    def ingest_oi_tick(self, strike: int, oi: int):
        """Records an OI observation for a given strike."""
        if strike not in self._oi_history:
            self._oi_history[strike] = collections.deque(maxlen=OI_WINDOW_TICKS)
        self._oi_history[strike].append((time.time(), oi))

    # ── OI Acceleration ───────────────────────────────────────────────────────

    def _compute_oi_acceleration(self, strike: int) -> float:
        """
        Returns how many times the LATEST OI reading is vs the 10-min rolling average.
        A value of 3.0 means OI is 300% of the average (threshold = 3.0).
        """
        history = self._oi_history.get(strike)
        if not history or len(history) < 10:
            return 0.0

        cutoff = time.time() - OI_WINDOW_MIN * 60
        window = [oi for ts, oi in history if ts >= cutoff]
        if not window:
            return 0.0

        avg_oi = sum(window) / len(window)
        latest_oi = window[-1]
        return latest_oi / avg_oi if avg_oi > 0 else 0.0

    def _find_atm_strikes(self, spot: float) -> list[int]:
        """Returns ATM ± 100 pt strikes (rounded to 50pt grid for Nifty)."""
        atm = int(round(spot / 50) * 50)
        return [atm - 100, atm - 50, atm, atm + 50, atm + 100]

    # ── Entry Evaluation ──────────────────────────────────────────────────────

    async def evaluate(self, state: dict) -> tuple[bool, int | None]:
        """Returns (should_enter, target_strike)."""
        if not self._active or self._position > 0:
            return False, None

        # Guard: DTE
        try:
            dte = int(self._redis.get("current_dte") or 99)
        except (TypeError, ValueError):
            dte = 99
        if dte > DTE_MAX:
            return False, None

        spot = state.get("spot", 0.0)
        strikes = self._find_atm_strikes(spot)

        # Find highest-accelerating strike
        best_strike = None
        best_accel = 0.0
        for strike in strikes:
            accel = self._compute_oi_acceleration(strike)
            if accel > OI_ACCEL_THRESHOLD and accel > best_accel:
                # Update price history
                self._price_history.append((time.time(), spot))
                # Spot must be trending TOWARD this strike
                recent_prices = [p for ts, p in self._price_history]
                if len(recent_prices) >= 2:
                    trending_toward = (
                        (strike > spot and recent_prices[-1] > recent_prices[0]) or
                        (strike < spot and recent_prices[-1] < recent_prices[0])
                    )
                    if trending_toward:
                        best_strike = strike
                        best_accel = accel
                else:
                    best_strike = strike
                    best_accel = accel

        if best_strike is None:
            return False, None

        return True, best_strike

    # ── Invalidation Check ─────────────────────────────────────────────────────

    def check_invalidation(self, spot: float) -> str | None:
        """Returns invalidation reason if spot has breached OI wall for > 30s."""
        if self._position == 0:
            return None

        strikes = self._find_atm_strikes(spot)
        now = time.time()

        for strike in strikes:
            accel = self._compute_oi_acceleration(strike)
            if accel < OI_ACCEL_THRESHOLD:
                continue

            # Check if spot is beyond the OI wall
            wall_breached = (
                (spot > strike + 20) or  # Spot > Call OI wall + buffer
                (spot < strike - 20)     # Spot < Put OI wall - buffer
            )
            if wall_breached:
                if strike not in self._wall_breach_times:
                    self._wall_breach_times[strike] = now
                elif now - self._wall_breach_times[strike] >= BREACH_TIMEOUT_SEC:
                    return f"OI_WALL_BREACH: Spot {spot:.0f} beyond strike {strike} for >{BREACH_TIMEOUT_SEC}s"
            else:
                self._wall_breach_times.pop(strike, None)  # Reset if returned to wall

        return None

    # ── Order Dispatch ────────────────────────────────────────────────────────

    async def dispatch_buy(self, state: dict, strike: int, lot_size: int):
        spot = state.get("spot", 0.0)
        # Build option symbol: NIFTY + expiry + strike + CE/PE
        option_side = "CE" if strike >= spot else "PE"
        symbol = f"NIFTY_ATM_{option_side}"

        # --- Hard Global Budget Check ---
        required_margin = spot * lot_size
        if not self._margin.reserve(required_margin):
            logger.error(f"❌ MARGIN REJECTED: {symbol} needs ₹{required_margin:,.2f} but budget is exhausted.")
            return None

        order_id = str(uuid.uuid4())

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "action": "BUY",
            "quantity": lot_size,
            "order_type": "MARKET",
            "strategy_id": self.STRATEGY_ID,
            "execution_type": "Paper",
            "price": spot,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dispatch_time_epoch": time.time(),
        }
        self._redis.hset("pending_orders", order_id, json.dumps({**order, "broker_order_id": None}))
        await self._mq.send_json(self._push, order, topic=Topics.ORDER_INTENT)
        self._position += lot_size
        self._entry_price = spot
        self._entry_time = time.time()
        logger.info(f"✅ STRAT_OI_PULSE BUY: {lot_size} lots {symbol} @ {spot:.0f} | Strike={strike}")
        return order_id

    async def _issue_orphan(self):
        await self._mq.send_json(self._push, {
            "symbol": "NIFTY_ATM_CE",
            "action": "BUY",
            "quantity": self._position,
            "price": self._entry_price,
            "strategy_id": self.STRATEGY_ID,
            "command": "ORPHAN",
            "execution_type": "Paper"
        }, topic="ORPHAN")
        self._position = 0


async def run_strategy():
    mq = MQManager()
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    strategy = OIPulseStrategy(r, mq)

    cmd_sub = mq.create_subscriber(Ports.SYSTEM_CMD, topics=[OIPulseStrategy.STRATEGY_ID])
    tick_sub = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.NIFTY50"])
    state_sub = mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE"])

    lot_size = int(r.hget("lot_sizes", "NIFTY50") or 65)

    async def cmd_handler():
        while True:
            _, cmd = await mq.recv_json(cmd_sub)
            if cmd:
                await strategy.handle_command(cmd)

    async def tick_handler():
        while True:
            _, tick = await mq.recv_json(tick_sub)
            if tick:
                spot = tick.get("price", 0.0)
                strikes = strategy._find_atm_strikes(spot)
                oi = tick.get("oi", 0)
                for strike in strikes:
                    strategy.ingest_oi_tick(strike, oi)

                # Invalidation check
                inv = strategy.check_invalidation(spot)
                if inv:
                    logger.warning(f"STRAT_OI_PULSE INVALIDATION: {inv}")
                    await strategy._issue_orphan()

    async def state_handler():
        nonlocal lot_size
        while True:
            _, state = await mq.recv_json(state_sub)
            if state:
                lot_size = int(r.hget("lot_sizes", "NIFTY50") or lot_size)
                day = datetime.now().strftime("%A")
                sized_lots = lot_size if day in ("Wednesday", "Thursday") else max(1, lot_size // 2)
                should_enter, strike = await strategy.evaluate(state)
                if should_enter and strike:
                    await strategy.dispatch_buy(state, strike, sized_lots)

    await asyncio.gather(cmd_handler(), tick_handler(), state_handler())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_strategy())
