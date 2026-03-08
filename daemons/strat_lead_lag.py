"""
daemons/strat_lead_lag.py
==========================
Strategy 5: Lead-Lag Reversion (SRS §3.5)

Regime: All regimes (correlation disruption creates mean-reversion opportunity)
Entry:
  - 1-minute Nifty/BankNifty correlation < 0.40 (dislocation)
  - Ratio Z-score (Nifty/BankNifty) > |2.5| — buy ATM of the laggard index
  - Volume confirmation: laggard volume > 120% of its 10-min average
Invalidation:
  - Correlation snaps back > 0.80 → reversion complete, exit immediately
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

import numpy as np
import redis

from core.mq import MQManager, Ports, Topics
from core.margin import MarginManager

logger = logging.getLogger("StratLeadLag")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    stream=sys.stdout)

# Thresholds
CORR_LOW = 0.40    # Below this → dislocation entry window
CORR_HIGH = 0.80   # Above this → reversion complete → exit
RATIO_Z_THRESHOLD = 2.5
VOL_CONFIRM_PCT = 1.20   # Laggard volume > 120% of its 10-min avg
CORR_WINDOW = 60         # Ticks for 1-min rolling correlation
VOL_WINDOW = 600         # Ticks for 10-min rolling volume


class LeadLagReversionStrategy:
    """Buy-only lead-lag reversion — buys ATM of the laggard index."""

    STRATEGY_ID = "STRAT_LEAD_LAG"

    def __init__(self, redis_client: redis.Redis, mq: MQManager):
        self._redis = redis_client
        self._mq = mq
        self._push = mq.create_push(Ports.ORDERS, bind=False)
        self._margin = MarginManager(self._redis)
        self._active = False
        self._position = 0
        self._entry_price = 0.0
        self._laggard_symbol = ""

        # Rolling price buffers per symbol
        self._prices: dict[str, collections.deque] = {
            "NIFTY50": collections.deque(maxlen=CORR_WINDOW),
            "BANKNIFTY": collections.deque(maxlen=CORR_WINDOW),
        }
        # Rolling volume buffers per symbol
        self._volumes: dict[str, collections.deque] = {
            "NIFTY50": collections.deque(maxlen=VOL_WINDOW),
            "BANKNIFTY": collections.deque(maxlen=VOL_WINDOW),
        }

    # ── Tick Ingestion ────────────────────────────────────────────────────────

    def ingest_tick(self, tick: dict):
        symbol = tick.get("symbol", "")
        if symbol in self._prices:
            self._prices[symbol].append(tick.get("price", 0.0))
            self._volumes[symbol].append(tick.get("volume", 0))

    # ── Signal Computation ────────────────────────────────────────────────────

    def _compute_correlation(self) -> float:
        """Rolling 1-min Pearson correlation between Nifty and BankNifty."""
        n_prices = np.array(list(self._prices["NIFTY50"]))
        b_prices = np.array(list(self._prices["BANKNIFTY"]))
        min_len = min(len(n_prices), len(b_prices))
        if min_len < 10:
            return 1.0  # Default: fully correlated (no signal)
        n_arr = n_prices[-min_len:]
        b_arr = b_prices[-min_len:]
        if np.std(n_arr) == 0 or np.std(b_arr) == 0:
            return 1.0
        return float(np.corrcoef(n_arr, b_arr)[0, 1])

    def _compute_ratio_zscore(self) -> tuple[float, str]:
        """
        Computes Z-score of Nifty/BankNifty price ratio.
        Also identifies the LAGGARD (the index further from its mean).
        Returns (z_score, laggard_symbol).
        """
        n_prices = np.array(list(self._prices["NIFTY50"]))
        b_prices = np.array(list(self._prices["BANKNIFTY"]))
        min_len = min(len(n_prices), len(b_prices))
        if min_len < 20:
            return 0.0, ""

        ratios = n_prices[-min_len:] / (b_prices[-min_len:] + 1e-8)
        mu = np.mean(ratios)
        sigma = np.std(ratios)
        z = float((ratios[-1] - mu) / sigma) if sigma > 0 else 0.0

        # Laggard: Nifty lags if ratio is HIGH (BankNifty is leading up)
        # Laggard: BankNifty lags if ratio is LOW (Nifty is leading up)
        if z > RATIO_Z_THRESHOLD:
            laggard = "BANKNIFTY"
        elif z < -RATIO_Z_THRESHOLD:
            laggard = "NIFTY50"
        else:
            laggard = ""

        return z, laggard

    def _check_volume_confirm(self, symbol: str) -> bool:
        """True if latest volume > 120% of 10-min rolling average."""
        vols = list(self._volumes.get(symbol, []))
        if len(vols) < 20:
            return True  # Not enough data, allow
        avg_vol = sum(vols) / len(vols)
        latest_vol = vols[-1]
        return latest_vol >= avg_vol * VOL_CONFIRM_PCT if avg_vol > 0 else True

    # ── Command Handler ───────────────────────────────────────────────────────

    async def handle_command(self, cmd: dict):
        command = cmd.get("command", "")
        if command == "ACTIVATE":
            if not self._active:
                logger.info("STRAT_LEAD_LAG: ACTIVATED")
            self._active = True
        elif command in ("PAUSE", "ORPHAN"):
            if self._active and self._position > 0:
                await self._issue_orphan()
            self._active = False
            logger.info(f"STRAT_LEAD_LAG: {command}")

    # ── Entry Evaluation ──────────────────────────────────────────────────────

    async def evaluate(self, state: dict) -> tuple[bool, str]:
        """Returns (should_enter, laggard_symbol)."""
        if not self._active or self._position > 0:
            return False, ""

        # Guard 1: Correlation dislocation
        corr = self._compute_correlation()
        if corr >= CORR_LOW:
            return False, ""

        # Guard 2: Ratio Z-score
        z, laggard = self._compute_ratio_zscore()
        if not laggard:
            return False, ""

        # Guard 3: Volume confirmation for laggard
        if not self._check_volume_confirm(laggard):
            return False, ""

        logger.debug(f"LEAD_LAG: corr={corr:.3f} z={z:.2f} laggard={laggard}")
        return True, laggard

    # ── Invalidation Check ────────────────────────────────────────────────────

    def check_invalidation(self) -> str | None:
        """Returns invalidation reason if correlation has snapped back > 0.80."""
        if self._position == 0:
            return None
        corr = self._compute_correlation()
        if corr > CORR_HIGH:
            return f"CORR_SNAPBACK: {corr:.3f} > {CORR_HIGH} threshold"
        return None

    # ── Order Dispatch ────────────────────────────────────────────────────────

    async def dispatch_buy(self, state: dict, laggard: str, lot_size: int):
        spot_key = "spot" if laggard == "NIFTY50" else "banknifty_spot"
        spot = state.get(spot_key, state.get("spot", 0.0))
        lot_symbol = "NIFTY50" if laggard == "NIFTY50" else "BANKNIFTY"
        symbol = f"{lot_symbol}_ATM_CE"

        # Lot sizes differ by index
        if laggard == "BANKNIFTY":
            lot_size = int(self._redis.hget("lot_sizes", "BANKNIFTY") or 30)
        else:
            lot_size = int(self._redis.hget("lot_sizes", "NIFTY50") or lot_size)

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
        self._laggard_symbol = laggard

        corr = self._compute_correlation()
        z, _ = self._compute_ratio_zscore()
        logger.info(f"✅ STRAT_LEAD_LAG BUY: {lot_size}L {symbol} @ {spot:.0f} "
                    f"| corr={corr:.3f} z={z:.2f} laggard={laggard}")
        return order_id

    async def _issue_orphan(self):
        symbol = f"{self._laggard_symbol or 'NIFTY50'}_ATM_CE"
        await self._mq.send_json(self._push, {
            "symbol": symbol,
            "action": "BUY",
            "quantity": self._position,
            "price": self._entry_price,
            "strategy_id": self.STRATEGY_ID,
            "command": "ORPHAN",
            "execution_type": "Paper"
        }, topic="ORPHAN")
        self._position = 0
        self._laggard_symbol = ""


async def run_strategy():
    mq = MQManager()
    r = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
    strategy = LeadLagReversionStrategy(r, mq)

    cmd_sub = mq.create_subscriber(Ports.SYSTEM_CMD, topics=[LeadLagReversionStrategy.STRATEGY_ID])
    tick_sub = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.NIFTY50", "TICK.BANKNIFTY"])
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
                strategy.ingest_tick(tick)
                inv = strategy.check_invalidation()
                if inv:
                    logger.warning(f"STRAT_LEAD_LAG INVALIDATION: {inv}. Orphaning.")
                    await strategy._issue_orphan()

    async def state_handler():
        nonlocal lot_size
        while True:
            _, state = await mq.recv_json(state_sub)
            if state:
                lot_size = int(r.hget("lot_sizes", "NIFTY50") or lot_size)
                day = datetime.now().strftime("%A")
                sized_lots = lot_size if day in ("Wednesday", "Thursday") else max(1, lot_size // 2)
                should_enter, laggard = await strategy.evaluate(state)
                if should_enter and laggard:
                    await strategy.dispatch_buy(state, laggard, sized_lots)

    await asyncio.gather(cmd_handler(), tick_handler(), state_handler())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_strategy())
