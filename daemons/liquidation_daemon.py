"""
daemons/liquidation_daemon.py
==============================
Liquidation Daemon & Granular Exception Handler (SRS §4.2)

Three barrier system:
  Barrier 1: TP = Entry + 2.5×ATR, SL = Entry - 1.0×ATR
  Barrier 2: Time-decaying aggressiveness (5min stall → cross bid-ask)
  Barrier 2b: HTTP 400 granular JSON parse (circuit limit → re-fire; margin/payload → abort+alert)
  Barrier 3: Sustained CVD Override (5-10 consecutive flip ticks → market sell)
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
import os

import redis.asyncio as redis

from core.mq import MQManager, Ports, Topics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("LiquidationDaemon")

# Barrier thresholds
ATR_TP_MULTIPLIER = 2.5
ATR_SL_MULTIPLIER = 1.0
STALL_TIMEOUT_SEC = 300       # 5 minutes
CVD_FLIP_EXIT_THRESHOLD = 5   # consecutive CVD flips → market exit
BARRIER2_SPREAD_CROSS_PCT = 0.10  # Cross bid-ask by 10% increments per retry

# HTTP 400 emsg substrings that indicate CIRCUIT LIMIT (safe to re-fire)
CIRCUIT_LIMIT_STRINGS = ["price range", "circuit", "freeze", "price band", "pcl", "ucl"]
# emsg substrings that indicate ABORT (don't re-fire)
ABORT_STRINGS = ["margin", "insufficient", "invalid", "malformed", "not found", "order not found"]


class LiquidationDaemon:
    def __init__(self):
        self.mq = MQManager()
        # Use PUSH so this daemon can coexist with live_bridge's PULL binding
        self.order_pub = self.mq.create_push(Ports.ORDERS, bind=False)

        # {symbol: {qty, entry_price, action, entry_time, strategy_id, execution_type}}
        self.orphaned_positions: dict[str, dict] = {}
        self._redis: redis.Redis | None = None

    # ── Startup ──────────────────────────────────────────────────────────────

    async def run(self):
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self._redis = redis.from_url(f"redis://{redis_host}:6379", decode_responses=True)
        logger.info("LiquidationDaemon active. Three-barrier system armed.")

        await asyncio.gather(
            self._handoff_listener(),
            self._market_monitor(),
        )

    # ── Handoff Listener ─────────────────────────────────────────────────────

    async def _handoff_listener(self):
        """Listens for ORPHAN/HANDOFF commands from strategy engine."""
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["ORPHAN", "HANDOFF"])
        while True:
            try:
                _, msg = await self.mq.recv_json(sub)
                if msg:
                    symbol = msg.get("symbol", "NIFTY_ATM_CE")
                    msg["entry_time"] = time.time()
                    msg["stall_retries"] = 0
                    self.orphaned_positions[symbol] = msg
                    logger.info(
                        f"Accepted ORPHAN: {symbol} Qty={msg.get('quantity')} "
                        f"Entry={msg.get('price', 0):.2f}"
                    )
            except Exception as e:
                logger.error(f"Handoff listener error: {e}")
            await asyncio.sleep(0.05)

    # ── Market Monitor ────────────────────────────────────────────────────────

    async def _market_monitor(self):
        """Polls market state and tick data to apply exit barriers."""
        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK.NIFTY50"])
        state_sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=["STATE"])

        latest_state: dict = {}

        async def _state_poller():
            nonlocal latest_state
            while True:
                try:
                    _, state = await self.mq.recv_json(state_sub)
                    if state:
                        latest_state = state
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        asyncio.create_task(_state_poller())

        while True:
            try:
                _, tick = await self.mq.recv_json(market_sub)
                if tick and self.orphaned_positions:
                    for symbol in list(self.orphaned_positions.keys()):
                        await self._evaluate_barriers(symbol, tick, latest_state)
            except Exception as e:
                logger.error(f"Market monitor error: {e}")
                await asyncio.sleep(0.5)

    # ── Barrier Evaluation ────────────────────────────────────────────────────

    async def _evaluate_barriers(self, symbol: str, tick: dict, state: dict):
        pos = self.orphaned_positions.get(symbol)
        if not pos:
            return

        price = tick.get("price", 0.0)
        entry = pos.get("price", price)
        elapsed = time.time() - pos.get("entry_time", time.time())
        action = pos.get("action", "BUY")

        # ── Barrier 1: ATR-mapped TP/SL ──────────────────────────────────────
        try:
            atr = float(await self._redis.get("atr") or 20.0)
        except Exception:
            atr = 20.0
        tp = entry + ATR_TP_MULTIPLIER * atr
        sl = entry - ATR_SL_MULTIPLIER * atr

        exit_reason = None
        if action == "BUY":
            if price >= tp:
                exit_reason = f"TP_HIT: {price:.2f} >= {tp:.2f}"
            elif price <= sl:
                exit_reason = f"SL_HIT: {price:.2f} <= {sl:.2f}"
        # (Buy-only system — no SELL positions to check inverse)

        # ── Barrier 2: Time-decaying aggressiveness ───────────────────────────
        if not exit_reason and elapsed >= STALL_TIMEOUT_SEC:
            pos["stall_retries"] = pos.get("stall_retries", 0) + 1
            retries = pos["stall_retries"]
            # Progressively cross bid-ask spread
            ask = tick.get("ask", price + 0.5)
            aggressive_price = ask + (ask - tick.get("bid", price - 0.5)) * BARRIER2_SPREAD_CROSS_PCT * retries
            exit_reason = f"STALL_TIMEOUT: {elapsed:.0f}s | aggressive_px={aggressive_price:.2f}"
            logger.warning(f"Barrier 2 triggered for {symbol}. Retry #{retries} @ {aggressive_price:.2f}")
            await self._attempt_exit(pos, symbol, price=aggressive_price, reason=exit_reason)
            return  # Don't also refire below

        # ── Barrier 3: Sustained CVD Override ────────────────────────────────
        if not exit_reason:
            try:
                cvd_flips = int(await self._redis.get("cvd_flip_ticks") or 0)
            except Exception:
                cvd_flips = 0
            if cvd_flips >= CVD_FLIP_EXIT_THRESHOLD:
                exit_reason = f"CVD_STRUCTURAL_FLIP: {cvd_flips} consecutive ticks"

        if exit_reason:
            logger.info(f"LIQUIDATION [{symbol}]: {exit_reason}")
            await self._attempt_exit(pos, symbol, price=price, reason=exit_reason)

    # ── Exit Execution with HTTP 400 Handling ────────────────────────────────

    async def _attempt_exit(self, pos: dict, symbol: str, price: float, reason: str):
        """
        Fires a SELL order. For HTTP 400 errors, parses emsg:
          - Circuit limit strings → recalculate bounds and re-fire
          - Margin/payload errors → abort + Telegram alert
        """
        qty = abs(pos.get("quantity", 0))
        if qty == 0:
            self.orphaned_positions.pop(symbol, None)
            return

        order = {
            "order_id": str(uuid.uuid4()),
            "symbol": symbol,
            "action": "SELL",
            "quantity": qty,
            "order_type": "MARKET",
            "strategy_id": "LIQUIDATION",
            "execution_type": pos.get("execution_type", "Paper"),
            "price": price,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason
        }

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                await self.mq.send_json(self.order_pub, order, topic=Topics.ORDER_INTENT)
                logger.info(f"✅ EXIT ORDER sent: SELL {qty} {symbol} @ {price:.2f} ({reason})")
                self.orphaned_positions.pop(symbol, None)
                return
            except Exception as e:
                err_str = str(e).lower()
                http400_result = self._parse_http400_emsg(err_str)

                if http400_result == "circuit_limit":
                    # Safe to recalculate and re-fire
                    logger.warning(f"HTTP 400 circuit limit. Recalculating price bounds. Attempt {attempt}.")
                    order["price"] = price * (0.98 if attempt % 2 == 0 else 1.02)
                    await asyncio.sleep(0.5 * attempt)

                elif http400_result == "abort":
                    logger.error(f"HTTP 400 ABORT (margin/payload error): {err_str}")
                    await self._telegram_alert(
                        f"🆘 CRITICAL: EXIT ORDER ABORTED for {symbol}\n"
                        f"Error: {err_str[:200]}\nManual intervention required!"
                    )
                    self.orphaned_positions.pop(symbol, None)
                    return
                else:
                    logger.error(f"Exit order error (attempt {attempt}): {e}")
                    await asyncio.sleep(1)

        # All retries exhausted
        logger.error(f"❌ EXIT ORDER FAILED after {max_retries} attempts for {symbol}.")
        await self._telegram_alert(
            f"❌ EXIT ORDER EXHAUSTED: {symbol} | {reason}\nManual intervention required!"
        )

    def _parse_http400_emsg(self, err_str: str) -> str:
        """
        Parses HTTP 400 error message to determine re-fire strategy.
        Returns: 'circuit_limit' | 'abort' | 'retry'
        """
        err_lower = err_str.lower()
        for pattern in CIRCUIT_LIMIT_STRINGS:
            if pattern in err_lower:
                return "circuit_limit"
        for pattern in ABORT_STRINGS:
            if pattern in err_lower:
                return "abort"
        return "retry"

    # ── Telegram Alert ────────────────────────────────────────────────────────

    async def _telegram_alert(self, message: str):
        try:
            await self._redis.lpush("telegram_alerts", json.dumps({
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "LIQUIDATION"
            }))
        except Exception as e:
            logger.error(f"Telegram alert failed: {e}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    daemon = LiquidationDaemon()
    asyncio.run(daemon.run())
