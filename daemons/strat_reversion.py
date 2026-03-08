"""
daemons/strat_reversion.py
==========================
Strategy 2: Institutional Fade / Mean Reversion (SRS §3.2)

Regime: POSITIVE GEX + HMM = RANGING
Entry:
  - Spot Z-Score > |2.5| from 15-minute mean
  - CVD Absorption Trigger confirmed
Invalidation:
  - Bid-Ask spread expands > 300% (liquidity vacuum)
  - log-OFI spikes > +3.0 against the position
"""

import asyncio
import collections
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone

import redis

from core.mq import MQManager, Ports, Topics
from core.margin import MarginManager

logger = logging.getLogger("StratReversion")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    stream=sys.stdout)


class InstitutionalFadeStrategy:
    """Buy-only mean reversion strategy targeting extended Z-score snapbacks."""

    STRATEGY_ID = "STRAT_REVERSION"
    SPOT_Z_THRESHOLD = 2.5     # Absolute Z-score from 15-min mean
    OFI_INVALIDATION = 3.0     # log-OFI Z against position → exit
    SPREAD_EXPANSION_FACTOR = 3.0  # 300% spread expansion → liquidity vacuum

    def __init__(self, redis_client: redis.Redis, mq: MQManager, lot_override: int | None = None):
        self._redis = redis_client
        self._mq = mq
        self._push = mq.create_push(Ports.ORDERS, bind=False)
        self._margin = MarginManager(self._redis)
        self._active = False
        self._position = 0
        self._entry_price = 0.0
        self._entry_time = 0.0
        self._lot_override = lot_override  # From dispersion veto: cap to 1 lot
        self._spread_baseline: float = 0.5  # Updated on first entry

    # ── Command Handler ───────────────────────────────────────────────────────

    async def handle_command(self, cmd: dict):
        command = cmd.get("command", "")
        if command == "ACTIVATE":
            self._lot_override = cmd.get("lot_override")
            if not self._active:
                logger.info(f"STRAT_REVERSION: ACTIVATED (lot_override={self._lot_override})")
            self._active = True
        elif command in ("PAUSE", "ORPHAN"):
            if self._active and self._position > 0:
                await self._issue_orphan()
            self._active = False
            logger.info(f"STRAT_REVERSION: {command}")

    # ── Entry Evaluation ──────────────────────────────────────────────────────

    async def evaluate(self, state: dict) -> bool:
        if not self._active or self._position > 0:
            return False

        # Regime guard
        if self._redis.get("gex_sign") != "POSITIVE":
            return False
        if self._redis.get("hmm_regime") != "RANGING":
            return False

        # Guard 1: Spot Z-score > |2.5| vs 15-min mean
        try:
            spot_z = float(state.get("spot_zscore_15m") or
                           self._redis.get("spot_zscore_15m") or 0.0)
        except (TypeError, ValueError):
            spot_z = 0.0
        if abs(spot_z) < self.SPOT_Z_THRESHOLD:
            return False

        # Guard 2: CVD Absorption confirmed
        cvd_flag = self._redis.get("cvd_absorption")
        if cvd_flag != "1":
            return False

        return True

    # ── Invalidation Check ────────────────────────────────────────────────────

    def check_invalidation(self, state: dict) -> str | None:
        """Returns invalidation reason string if position should be closed, else None."""
        if self._position == 0:
            return None

        # Check spread expansion (liquidity vacuum)
        try:
            latest_tick_raw = self._redis.get("latest_tick:NIFTY50")
            if latest_tick_raw:
                latest_tick = json.loads(latest_tick_raw)
                spread = latest_tick.get("ask", 0) - latest_tick.get("bid", 0)
                if self._spread_baseline > 0 and spread > self._spread_baseline * self.SPREAD_EXPANSION_FACTOR:
                    return f"LIQUIDITY_VACUUM: spread {spread:.2f} > {self._spread_baseline * 3:.2f}"
        except Exception:
            pass

        # Check OFI spike against position
        try:
            ofi_z = float(state.get("log_ofi_zscore") or self._redis.get("log_ofi_zscore") or 0.0)
            # For a BUY (expecting price to revert UP), OFI > +3.0 means MORE buying
            # but if spot_z was < -2.5 (oversold), we bought — OFI spike in opposite direction:
            spot_z = float(state.get("spot_zscore_15m") or 0.0)
            if spot_z < -2.5 and ofi_z > self.OFI_INVALIDATION:
                # Continued selling pressure against our long
                return f"OFI_SPIKE_AGAINST_POSITION: {ofi_z:.2f}"
        except Exception:
            pass

        return None

    # ── Order Dispatch ────────────────────────────────────────────────────────

    async def dispatch_buy(self, state: dict, lot_size: int):
        effective_lots = 1 if self._lot_override == 1 else lot_size
        symbol = state.get("atm_option_symbol", "NIFTY_ATM_CE")
        spot = state.get("spot", 0.0)

        # --- Hard Global Budget Check ---
        required_margin = spot * effective_lots
        if not self._margin.reserve(required_margin):
            logger.error(f"❌ MARGIN REJECTED: {symbol} needs ₹{required_margin:,.2f} but budget is exhausted.")
            return None

        order_id = str(uuid.uuid4())

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "action": "BUY",
            "quantity": effective_lots,
            "order_type": "MARKET",
            "strategy_id": self.STRATEGY_ID,
            "execution_type": "Paper",
            "price": spot,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dispatch_time_epoch": time.time(),
        }

        # Set spread baseline on entry
        try:
            tick_raw = self._redis.get("latest_tick:NIFTY50")
            if tick_raw:
                tick = json.loads(tick_raw)
                self._spread_baseline = max(0.05, tick.get("ask", spot + 0.5) - tick.get("bid", spot - 0.5))
        except Exception:
            self._spread_baseline = 0.5

        self._redis.hset("pending_orders", order_id, json.dumps({**order, "broker_order_id": None}))
        await self._mq.send_json(self._push, order, topic=Topics.ORDER_INTENT)
        self._position += effective_lots
        self._entry_price = spot
        self._entry_time = time.time()
        logger.info(f"✅ STRAT_REVERSION BUY: {effective_lots} lots {symbol} @ {spot:.0f} "
                    f"(z={abs(state.get('spot_zscore_15m', 0.0)):.2f})")
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
    strategy = InstitutionalFadeStrategy(r, mq)

    cmd_sub = mq.create_subscriber(Ports.SYSTEM_CMD, topics=[InstitutionalFadeStrategy.STRATEGY_ID])
    state_sub = mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE"])

    lot_size = int(r.hget("lot_sizes", "NIFTY50") or 65)

    async def cmd_handler():
        while True:
            _, cmd = await mq.recv_json(cmd_sub)
            if cmd:
                await strategy.handle_command(cmd)

    async def state_handler():
        nonlocal lot_size
        while True:
            _, state = await mq.recv_json(state_sub)
            if state:
                lot_size = int(r.hget("lot_sizes", "NIFTY50") or lot_size)
                day = datetime.now().strftime("%A")
                sized_lots = lot_size if day in ("Wednesday", "Thursday") else max(1, lot_size // 2)

                # Check invalidation first
                inv_reason = strategy.check_invalidation(state)
                if inv_reason:
                    logger.warning(f"STRAT_REVERSION INVALIDATION: {inv_reason}. Issuing orphan.")
                    await strategy._issue_orphan()

                if await strategy.evaluate(state):
                    await strategy.dispatch_buy(state, sized_lots)

    await asyncio.gather(cmd_handler(), state_handler())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_strategy())
