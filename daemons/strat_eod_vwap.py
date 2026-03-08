"""
daemons/strat_eod_vwap.py
==========================
Strategy 3: Anchored VWAP (SRS §3.3)

Regime: High Alpha Score (|s_total| > 40)
Entry:
  - Anchor at 09:15 AM IST (re-anchor at 09:30 if opening gap > 1.0%)
  - Spot touches within ±0.2% of VWAP
  - Fast SMA crosses Slow SMA confirming bounce
Invalidation:
  - 1-minute Spot candle closes completely wrong side of VWAP

Uses Polars for all VWAP calculations (replaces legacy pandas).
"""

import asyncio
import collections
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

import polars as pl
import redis

from core.mq import MQManager, Ports, Topics
from core.margin import MarginManager

logger = logging.getLogger("StratVWAP")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    stream=sys.stdout)

IST = ZoneInfo("Asia/Kolkata")
ALPHA_THRESHOLD = 40.0
VWAP_TOUCH_PCT = 0.002   # ±0.2%
FAST_SMA_PERIOD = 5
SLOW_SMA_PERIOD = 20
OPENING_GAP_THRESHOLD = 0.01  # 1.0%


class AnchoredVWAPStrategy:
    """Buy-only anchored VWAP bounce strategy."""

    STRATEGY_ID = "STRAT_VWAP"

    def __init__(self, redis_client: redis.Redis, mq: MQManager):
        self._redis = redis_client
        self._mq = mq
        self._push = mq.create_push(Ports.ORDERS, bind=False)
        self._margin = MarginManager(self._redis)
        self._active = False
        self._position = 0
        self._entry_price = 0.0
        self._entry_time = 0.0

        # Tick accumulator: (timestamp_epoch, price, volume)
        self._ticks: list[tuple[float, float, float]] = []
        self._anchor_epoch: float = self._default_anchor()
        self._anchored_today: date | None = None

        # 1-min candle state for invalidation
        self._candle_open: float = 0.0
        self._candle_high: float = 0.0
        self._candle_low: float = 0.0
        self._candle_close: float = 0.0
        self._candle_start: float = 0.0

    def _default_anchor(self) -> float:
        """Default anchor = 09:15 IST today."""
        now = datetime.now(tz=IST)
        anchor = now.replace(hour=9, minute=15, second=0, microsecond=0)
        return anchor.timestamp()

    # ── Command Handler ───────────────────────────────────────────────────────

    async def handle_command(self, cmd: dict):
        command = cmd.get("command", "")
        if command == "ACTIVATE":
            if not self._active:
                logger.info("STRAT_VWAP: ACTIVATED")
            self._active = True
        elif command in ("PAUSE", "ORPHAN"):
            if self._active and self._position > 0:
                await self._issue_orphan()
            self._active = False
            logger.info(f"STRAT_VWAP: {command}")

    # ── VWAP Calculation (Polars) ─────────────────────────────────────────────

    def _compute_vwap(self) -> float | None:
        """Compute anchored VWAP using Polars (fast, zero-copy)."""
        anchor = self._anchor_epoch
        anchored_ticks = [(ts, p, v) for ts, p, v in self._ticks if ts >= anchor]
        if len(anchored_ticks) < 5:
            return None

        df = pl.DataFrame(
            anchored_ticks,
            schema={"ts": pl.Float64, "price": pl.Float64, "volume": pl.Float64},
            orient="row"
        )
        pv_sum = (df["price"] * df["volume"]).sum()
        v_sum = df["volume"].sum()
        if v_sum == 0:
            return None
        return float(pv_sum / v_sum)

    def _compute_sma(self, period: int) -> float | None:
        """Rolling SMA of last `period` prices."""
        prices = [p for _, p, _ in self._ticks[-period:]]
        if len(prices) < period:
            return None
        return sum(prices) / len(prices)

    # ── Re-anchor Logic ───────────────────────────────────────────────────────

    def _maybe_reanchor(self, first_price: float):
        """Re-anchors at 09:30 if opening gap > 1.0%."""
        now = datetime.now(tz=IST)
        if now.hour == 9 and now.minute == 30 and self._anchored_today != now.date():
            # Check previous day close (use first available tick as proxy)
            if self._ticks:
                first_open = self._ticks[0][1]
                if first_open > 0 and abs(first_price - first_open) / first_open >= OPENING_GAP_THRESHOLD:
                    new_anchor = now.replace(second=0, microsecond=0).timestamp()
                    logger.info(f"VWAP re-anchored at 09:30 (gap={abs(first_price - first_open)/first_open*100:.2f}%)")
                    self._anchor_epoch = new_anchor
                    self._anchored_today = now.date()

    # ── 1-Min Candle Management ───────────────────────────────────────────────

    def _update_candle(self, price: float, ts: float) -> bool:
        """Update 1-min candle. Returns True when a new candle starts (trigger invalidation check)."""
        if self._candle_start == 0 or ts - self._candle_start >= 60:
            self._candle_open = price
            self._candle_high = price
            self._candle_low = price
            self._candle_close = price
            self._candle_start = ts
            return True
        self._candle_high = max(self._candle_high, price)
        self._candle_low = min(self._candle_low, price)
        self._candle_close = price
        return False

    def _candle_closed_wrong_side(self, vwap: float, position: str = "LONG") -> bool:
        """True if last 1-min candle closed entirely below VWAP (for a long position)."""
        if position == "LONG":
            return self._candle_high < vwap  # entire candle below VWAP
        return self._candle_low > vwap

    # ── Entry Evaluation ──────────────────────────────────────────────────────

    async def evaluate(self, state: dict) -> bool:
        if not self._active or self._position > 0:
            return False

        s_total = abs(state.get("s_total", 0))
        if s_total < ALPHA_THRESHOLD:
            return False

        spot = state.get("spot", 0.0)
        vwap = self._compute_vwap()
        if vwap is None:
            return False

        # Guard: Spot within ±0.2% of VWAP
        if abs(spot - vwap) / vwap > VWAP_TOUCH_PCT:
            return False

        # Guard: Fast SMA crosses Slow SMA confirming bounce
        fast_sma = self._compute_sma(FAST_SMA_PERIOD)
        slow_sma = self._compute_sma(SLOW_SMA_PERIOD)
        if fast_sma is None or slow_sma is None:
            return False
        if fast_sma <= slow_sma:
            return False  # No bullish crossover yet

        logger.debug(f"VWAP={vwap:.2f} spot={spot:.2f} fast={fast_sma:.2f} slow={slow_sma:.2f}")
        return True

    def check_invalidation(self, vwap: float) -> bool:
        """True if position invalidated (1-min candle closed wrong side of VWAP)."""
        if self._position == 0 or vwap is None:
            return False
        return self._candle_closed_wrong_side(vwap, "LONG")

    # ── Tick Ingestion ────────────────────────────────────────────────────────

    def ingest_tick(self, tick: dict):
        ts = time.time()
        price = tick.get("price", 0.0)
        volume = float(tick.get("volume", 1))
        self._ticks.append((ts, price, volume))
        # Keep only last 2 hours (~7200 ticks at 1/s)
        if len(self._ticks) > 7200:
            self._ticks = self._ticks[-7200:]
        self._update_candle(price, ts)
        self._maybe_reanchor(price)

    # ── Order Dispatch ────────────────────────────────────────────────────────

    async def dispatch_buy(self, state: dict, lot_size: int):
        symbol = state.get("atm_option_symbol", "NIFTY_ATM_CE")
        spot = state.get("spot", 0.0)

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
        vwap = self._compute_vwap() or spot
        logger.info(f"✅ STRAT_VWAP BUY: {lot_size} lots {symbol} @ {spot:.0f} | VWAP={vwap:.2f}")
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
    strategy = AnchoredVWAPStrategy(r, mq)

    cmd_sub = mq.create_subscriber(Ports.SYSTEM_CMD, topics=[AnchoredVWAPStrategy.STRATEGY_ID])
    tick_sub = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.NIFTY50"])
    state_sub = mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE"])

    lot_size = int(r.hget("lot_sizes", "NIFTY50") or 65)
    latest_state: dict = {}

    async def cmd_handler():
        while True:
            _, cmd = await mq.recv_json(cmd_sub)
            if cmd:
                await strategy.handle_command(cmd)

    async def tick_handler():
        while True:
            _, tick = await mq.recv_json(tick_sub)
            if tick:
                strategy.ingest_tick(tick)
                vwap = strategy._compute_vwap()
                if vwap and strategy.check_invalidation(vwap):
                    logger.warning("STRAT_VWAP: 1-min candle CLOSED WRONG SIDE of VWAP. Orphaning.")
                    await strategy._issue_orphan()

    async def state_handler():
        nonlocal lot_size
        while True:
            _, state = await mq.recv_json(state_sub)
            if state:
                lot_size = int(r.hget("lot_sizes", "NIFTY50") or lot_size)
                day = datetime.now().strftime("%A")
                sized_lots = lot_size if day in ("Wednesday", "Thursday") else max(1, lot_size // 2)
                if await strategy.evaluate(state):
                    await strategy.dispatch_buy(state, sized_lots)

    await asyncio.gather(cmd_handler(), tick_handler(), state_handler())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_strategy())
