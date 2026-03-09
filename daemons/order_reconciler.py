"""
daemons/order_reconciler.py
===========================
Decoupled State Reconciliation Daemon (SRS §2.5)

Replaces the naive 200ms timeout with a dedicated, continuously-polling REST
loop that verifies EVERY dispatched order against the exchange's physical state.

Flow:
  1. Strategy engine pushes ORDER_INTENT ID to Redis hash "pending_orders"
     Format: HSET pending_orders <order_id> <json_metadata>
  2. This daemon polls Shoonya single_order_history() every 500ms per pending order.
  3. On confirmed execution  → removes from pending_orders, updates position state
  4. On "Phantom" (3+ sec unrecognized) → fires Telegram CRITICAL, cleans up state
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis
from dotenv import load_dotenv

from core.margin import AsyncMarginManager

try:
    import uvloop
except ImportError:
    uvloop = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("OrderReconciler")

# Timeouts
POLL_INTERVAL_MS = 500          # ms between per-order polls
PHANTOM_THRESHOLD_SEC = 3.0     # seconds before declaring phantom
MAX_RETRIES_PER_ORDER = 6       # 3 seconds / 0.5s polling = 6 retries

# Shoonya order status codes
STATUS_COMPLETE = "COMPLETE"
STATUS_REJECTED = "REJECTED"
STATUS_CANCELLED = "CANCELLED"
STATUS_OPEN = "OPEN"
STATUS_PENDING = "PENDING"
TERMINAL_STATUSES = {STATUS_COMPLETE, STATUS_REJECTED, STATUS_CANCELLED}


class OrderReconciler:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        load_dotenv()
        self.redis_url = redis_url
        self.redis: Optional[redis.Redis] = None
        self._retry_counts: dict[str, int] = {}  # order_id → retry count
        self._dispatch_times: dict[str, float] = {}  # order_id → epoch dispatch time

        # Shoonya API (lazy-loaded when available)
        self._api = None
        self._api_available = False
        self._init_broker_api()

    def _init_broker_api(self):
        """Attempts to load the Shoonya API. Falls back to mock in paper trading."""
        try:
            import hashlib
            from NorenRestApiPy.NorenApi import NorenApi  # type: ignore

            host = os.getenv("SHOONYA_HOST", "")
            if not host:
                logger.warning("SHOONYA_HOST not set. Reconciler running in MOCK mode.")
                return

            ws_url = host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP")
            if not ws_url.endswith("/"):
                ws_url += "/"
            self._api = NorenApi(host=host, websocket=ws_url)
            self._api_available = True
            logger.info("Reconciler: Shoonya API client initialized.")
        except ImportError:
            logger.warning("NorenRestApiPy not installed. Reconciler in MOCK mode.")
        except Exception as e:
            logger.error(f"API init error: {e}. Reconciler in MOCK mode.")

    # ── Main Entry ───────────────────────────────────────────────────────────

    async def start(self):
        self.redis = redis.from_url(self.redis_url, decode_responses=True)
        self._margin = AsyncMarginManager(self.redis)
        logger.info("OrderReconciler started. Polling pending_orders every 500ms.")

        while True:
            try:
                await self._reconciliation_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reconciliation cycle error: {e}")
            await asyncio.sleep(POLL_INTERVAL_MS / 1000.0)

        await self.redis.aclose()

    # ── Reconciliation Cycle ─────────────────────────────────────────────────

    async def _reconciliation_cycle(self):
        """One pass: checks every order in the pending_orders Redis hash."""
        pending = await self.redis.hgetall("pending_orders")
        if not pending:
            return

        # Run all order checks concurrently
        tasks = [
            self._check_order(order_id, meta_raw)
            for order_id, meta_raw in pending.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_order(self, order_id: str, meta_raw: str):
        """Verify a single pending order against the exchange."""
        try:
            meta = json.loads(meta_raw)
        except json.JSONDecodeError:
            logger.warning(f"Corrupt pending_orders entry for {order_id}. Removing.")
            await self.redis.hdel("pending_orders", order_id)
            return

        # Track dispatch time for phantom detection
        if order_id not in self._dispatch_times:
            self._dispatch_times[order_id] = meta.get("dispatch_time_epoch", time.time())
            self._retry_counts[order_id] = 0

        age_sec = time.time() - self._dispatch_times[order_id]
        broker_order_id = meta.get("broker_order_id")  # Shoonya norenordno (may be None if not yet ACK'd)

        # ── Poll broker status ────────────────────────────────────────────
        status, fill_price = await self._query_broker(broker_order_id, order_id)

        if status == STATUS_COMPLETE:
            logger.info(
                f"✅ CONFIRMED: Order {order_id} executed @ {fill_price}. "
                f"Updating state."
            )
            await self._mark_order_confirmed(order_id, meta, fill_price)
            return

        if status in (STATUS_REJECTED, STATUS_CANCELLED):
            logger.warning(f"❌ Order {order_id} terminal status: {status}. Cleaning up.")
            await self._mark_order_failed(order_id, meta, status)
            return

        # Still open/pending — check for phantom threshold
        self._retry_counts[order_id] = self._retry_counts.get(order_id, 0) + 1

        if age_sec >= PHANTOM_THRESHOLD_SEC:
            logger.error(
                f"👻 PHANTOM ORDER DETECTED: {order_id} unrecognized after "
                f"{age_sec:.1f}s. Flagging and cleaning up."
            )
            await self._handle_phantom(order_id, meta)

    # ── Broker Query ─────────────────────────────────────────────────────────

    async def _query_broker(self, broker_order_id: Optional[str], local_order_id: str) -> tuple[str, float]:
        """
        Queries single_order_history from Shoonya.
        Returns (status, fill_price). Falls back to MOCK if API unavailable.
        """
        if not self._api_available or not broker_order_id:
            # In paper trading / mock mode: simulate ~150ms network latency and
            # "COMPLETE" after 2nd retry to test the happy path.
            retries = self._retry_counts.get(local_order_id, 0)
            await asyncio.sleep(0.05)  # simulate latency
            if retries >= 2:
                return STATUS_COMPLETE, 0.0
            return STATUS_PENDING, 0.0

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: self._api.single_order_history(orderno=broker_order_id)
            )

            if not result or not isinstance(result, list):
                return STATUS_PENDING, 0.0

            # Shoonya returns list of history entries; latest is first
            latest = result[0]
            raw_status = latest.get("status", "").upper()

            # Map to our canonical statuses
            if raw_status in ("COMPLETE", "FILLED"):
                fill_price = float(latest.get("prc", 0) or latest.get("rprc", 0) or 0)
                return STATUS_COMPLETE, fill_price
            elif raw_status in ("REJECTED", "CANCELLED"):
                return STATUS_REJECTED if "REJECT" in raw_status else STATUS_CANCELLED, 0.0
            else:
                return STATUS_OPEN, 0.0

        except Exception as e:
            logger.debug(f"Broker query error for {broker_order_id}: {e}")
            return STATUS_PENDING, 0.0

    async def _cleanup_refinement_locks(self, order_id: str, symbol: str):
        """Clears Double-Tap Guard lock and Pending Journal (SRS §2.6, §2.7)."""
        await self.redis.delete(f"lock:{symbol}")
        await self.redis.delete(f"Pending_Journal:{order_id}")
        logger.debug(f"Cleaned refinement locks for {order_id} ({symbol})")

    # ── State Update Helpers ─────────────────────────────────────────────────

    async def _mark_order_confirmed(self, order_id: str, meta: dict, fill_price: float):
        """Remove from pending, update confirmed position state in Redis."""
        # Remove from pending
        await self.redis.hdel("pending_orders", order_id)
        self._dispatch_times.pop(order_id, None)
        self._retry_counts.pop(order_id, None)
        
        # Clear Refinement Locks
        await self._cleanup_refinement_locks(order_id, meta.get("symbol", "UNKNOWN"))

        # Publish confirmed fill for downstream consumers (liquidation daemon, dashboard)
        await self.redis.publish("order_confirmations", json.dumps({
            "order_id": order_id,
            "status": "CONFIRMED",
            "fill_price": fill_price,
            "symbol": meta.get("symbol"),
            "action": meta.get("action"),
            "quantity": meta.get("quantity"),
            "strategy_id": meta.get("strategy_id"),
            "execution_type": meta.get("execution_type"),
            "confirmed_at": datetime.now(timezone.utc).isoformat()
        }))

        # Update position in Redis for quick reads
        symbol = meta.get("symbol", "UNKNOWN")
        strat = meta.get("strategy_id", "UNKNOWN")
        qty_delta = meta.get("quantity", 0) if meta.get("action") == "BUY" else -meta.get("quantity", 0)
        pos_key = f"position:{symbol}:{strat}"
        await self.redis.incrbyfloat(pos_key, qty_delta)
        await self.redis.expire(pos_key, 86400)  # 24h TTL

        # Release Margin on SELL
        if meta.get("action") == "SELL":
            refund_amount = fill_price * abs(meta.get("quantity", 0))
            if refund_amount > 0:
                execution_type = meta.get("execution_type", "Paper")
                await self._margin.release(refund_amount, execution_type)
                logger.info(f"Released ₹{refund_amount:.2f} {execution_type} margin after SELL execution.")

    async def _mark_order_failed(self, order_id: str, meta: dict, status: str):
        """Clean up a rejected/cancelled order."""
        await self.redis.hdel("pending_orders", order_id)
        self._dispatch_times.pop(order_id, None)
        self._retry_counts.pop(order_id, None)
        
        # Clear Refinement Locks
        await self._cleanup_refinement_locks(order_id, meta.get("symbol", "UNKNOWN"))

        await self.redis.publish("order_confirmations", json.dumps({
            "order_id": order_id,
            "status": status,
            "symbol": meta.get("symbol"),
            "strategy_id": meta.get("strategy_id"),
            "failed_at": datetime.now(timezone.utc).isoformat()
        }))

        # Alert on rejection
        await self._telegram_alert(
            f"❌ Order {order_id} REJECTED/CANCELLED by exchange. "
            f"Symbol: {meta.get('symbol')} | Strategy: {meta.get('strategy_id')}"
        )

    async def _handle_phantom(self, order_id: str, meta: dict):
        """Handle a phantom order: local state believed dispatched but exchange has no record."""
        await self.redis.hdel("pending_orders", order_id)
        self._dispatch_times.pop(order_id, None)
        self._retry_counts.pop(order_id, None)
        
        # Clear Refinement Locks
        await self._cleanup_refinement_locks(order_id, meta.get("symbol", "UNKNOWN"))

        alert = (
            f"👻 PHANTOM ORDER: {order_id}\n"
            f"Symbol: {meta.get('symbol')} | Action: {meta.get('action')} "
            f"| Qty: {meta.get('quantity')} | Strategy: {meta.get('strategy_id')}\n"
            f"Exchange has no record. Local trade state cancelled."
        )
        logger.critical(alert)
        await self._telegram_alert(alert)

        # Push phantom event to Redis for dashboard
        await self.redis.lpush("phantom_orders", json.dumps({
            "order_id": order_id,
            "meta": meta,
            "detected_at": datetime.now(timezone.utc).isoformat()
        }))
        await self.redis.ltrim("phantom_orders", 0, 99)

        # Reverse reserved margin for failed entry
        if meta.get("action") == "BUY":
            refund_amount = meta.get("price", 0.0) * meta.get("quantity", 0)
            if refund_amount > 0:
                execution_type = meta.get("execution_type", "Paper")
                await self._margin.release(refund_amount, execution_type)
                logger.info(f"Refunded ₹{refund_amount:.2f} {execution_type} margin for phantom BUY order {order_id}.")

    # ── Telegram Alert Helper ────────────────────────────────────────────────

    async def _telegram_alert(self, message: str):
        try:
            await self.redis.lpush("telegram_alerts", json.dumps({
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "RECONCILER"
            }))
        except Exception as e:
            logger.error(f"Telegram alert push failed: {e}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    elif uvloop:
        uvloop.install()

    reconciler = OrderReconciler()
    asyncio.run(reconciler.start())
