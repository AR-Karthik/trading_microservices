"""
daemons/strat_gamma.py
======================
Strategy 1: Long Gamma Momentum (SRS §3.1)

Regime: NEGATIVE GEX + HMM = TRENDING
Entry:
  - DTE ≤ 2
  - Delta ∈ [0.45, 0.55]  (ATM options only)
  - log-OFI Z-Score > +2.0
  - Spot breaks 5-min high/low
  - Top-5 correlation > 0.50 (confirmed momentum)
Target: 20-30 pt structural run
Invalidation: HMM shifts TRENDING → RANGING
"""

import asyncio
import collections
import json
import logging
import sys
from datetime import datetime, timezone

import redis

from core.mq import MQManager, Ports, Topics
from core.margin import MarginManager

logger = logging.getLogger("StratGamma")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                    stream=sys.stdout)


class LongGammaMomentumStrategy:
    """Buy-only long gamma momentum options scalper."""

    STRATEGY_ID = "STRAT_GAMMA"
    TARGET_DELTA_LOW = 0.45
    TARGET_DELTA_HIGH = 0.55
    MAX_DTE = 2
    OFI_Z_THRESHOLD = 2.0
    CORR_THRESHOLD = 0.50
    BREAKOUT_WINDOW = 5 * 60  # 5-minute high/low window in seconds

    def __init__(self, redis_client: redis.Redis, mq: MQManager, execution_type: str = "Paper"):
        self._redis = redis_client
        self._mq = mq
        self._push = mq.create_push(Ports.ORDERS, bind=False)
        self._margin = MarginManager(self._redis)
        self.execution_type = execution_type
        self._active = False
        self._position = 0  # number of lots currently held
        self._entry_price: float = 0.0
        self._entry_time: float = 0.0
        self._price_window: collections.deque = collections.deque(maxlen=500)

    # ── Command Handler ───────────────────────────────────────────────────────

    async def handle_command(self, cmd: dict):
        command = cmd.get("command", "")
        if command == "ACTIVATE":
            if not self._active:
                logger.info("STRAT_GAMMA: ACTIVATED")
            self._active = True
        elif command in ("PAUSE", "ORPHAN"):
            if self._active and self._position > 0:
                logger.warning("STRAT_GAMMA: REGIME INVALIDATION — issuing ORPHAN")
                await self._issue_orphan()
            self._active = False
            logger.info(f"STRAT_GAMMA: {command}")

    # ── Entry Evaluation ──────────────────────────────────────────────────────

    async def evaluate(self, state: dict) -> bool:
        """Returns True if entry conditions are fully satisfied."""
        if not self._active or self._position > 0:
            return False

        # Guard 1: Regime (enforced upstream by MetaRouter — double-check Redis)
        if self._redis.get("gex_sign") != "NEGATIVE":
            return False
        if self._redis.get("hmm_regime") != "TRENDING":
            return False

        # Guard 2: DTE ≤ 2 (read from options chain snapshot in Redis)
        try:
            dte = int(self._redis.get("current_dte") or 99)
        except (TypeError, ValueError):
            dte = 99
        if dte > self.MAX_DTE:
            return False

        # Guard 3: ATM Delta ∈ [0.45, 0.55]
        try:
            delta = float(self._redis.get("atm_delta") or 0.5)
        except (TypeError, ValueError):
            delta = 0.5
        if not (self.TARGET_DELTA_LOW <= delta <= self.TARGET_DELTA_HIGH):
            return False

        # Guard 4: Log-OFI Z > +2.0
        try:
            ofi_z = float(self._redis.get("log_ofi_zscore") or 0.0)
        except (TypeError, ValueError):
            ofi_z = 0.0
        if ofi_z < self.OFI_Z_THRESHOLD:
            return False

        # Guard 5: Spot breaks 5-min high/low
        spot = state.get("spot", 0.0)
        if not self._check_breakout(spot):
            return False

        # Guard 6: Top-5 correlation > 0.50
        try:
            disp = float(self._redis.get("dispersion_coeff") or 0.5)
            # High dispersion_coeff means CORRELATED (Pearson ≈ 1.0 across stocks)
            if disp < self.CORR_THRESHOLD:
                return False
        except (TypeError, ValueError):
            pass

        return True

    def _check_breakout(self, spot: float) -> bool:
        """Checks if spot has broken the 5-minute high or low."""
        import time
        self._price_window.append((time.time(), spot))
        cutoff = time.time() - self.BREAKOUT_WINDOW
        recent = [p for ts, p in self._price_window if ts >= cutoff]
        if len(recent) < 10:
            return False
        window_high = max(recent[:-1])
        window_low = min(recent[:-1])
        return spot > window_high or spot < window_low

    # ── Order Dispatch ────────────────────────────────────────────────────────

    async def dispatch_buy(self, state: dict, lot_size: int):
        """Issues a BUY ORDER_INTENT with pending_orders registration."""
        import uuid, time

        symbol = state.get("atm_option_symbol", "NIFTY_ATM_CE")
        spot = state.get("spot", 0.0)
        
        # --- Hard Global Budget Check ---
        required_margin = spot * lot_size
        if not self._margin.reserve(required_margin, self.execution_type):
            logger.error(f"❌ MARGIN REJECTED: {symbol} needs ₹{required_margin:,.2f} but {self.execution_type} budget is exhausted.")
            # Veto the trade, do not dispatch
            return None

        order_id = str(uuid.uuid4())

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "action": "BUY",
            "quantity": lot_size,
            "order_type": "MARKET",
            "strategy_id": self.STRATEGY_ID,
            "execution_type": self.execution_type,
            "price": spot,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dispatch_time_epoch": time.time(),
        }

        # Register in pending_orders for reconciler
        self._redis.hset("pending_orders", order_id, json.dumps({
            **order, "broker_order_id": None
        }))

        await self._mq.send_json(self._push, order, topic=Topics.ORDER_INTENT)
        self._position += lot_size
        self._entry_price = spot
        self._entry_time = time.time()
        logger.info(f"✅ STRAT_GAMMA BUY: {lot_size} lots {symbol} @ {spot:.0f}")
        return order_id

    async def _issue_orphan(self):
        """Hands off open position to liquidation daemon."""
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
    strategy = LongGammaMomentumStrategy(r, mq)

    cmd_sub = mq.create_subscriber(Ports.SYSTEM_CMD, topics=[LongGammaMomentumStrategy.STRATEGY_ID])
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
                # DTE-based sizing: Wed/Thu=100%, Fri-Tue=50%
                day = datetime.now().strftime("%A")
                sized_lots = lot_size if day in ("Wednesday", "Thursday") else max(1, lot_size // 2)
                if await strategy.evaluate(state):
                    await strategy.dispatch_buy(state, sized_lots)

    await asyncio.gather(cmd_handler(), state_handler())


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_strategy())
