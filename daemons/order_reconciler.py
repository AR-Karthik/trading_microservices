"""
Order Reconciler — Multi-Leg Health Monitor
Refactored for:
  1. Sub-500ms Leg-Sync with Atomic Kill-Switch
  2. Batch Ghost Hunter with Leaky-Bucket Rate Limiter
  3. Remarks-based O(1) Basket Routing
  4. Redis Hash-based Shadow Ledger (no JSON parsing)
  5. Terminal Hour Hyper-Drive (14:30–15:30 IST)
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import redis.asyncio as redis
import zmq
import zmq.asyncio

from core.mq import MQManager, Ports, Topics
from core.logger import setup_logger
from core.health import HeartbeatProvider
from core.margin import AsyncMarginManager

try:
    from NorenRestApiPy.NorenApi import NorenApi
    import pyotp
    _HAS_SHOONYA = True
except ImportError:
    _HAS_SHOONYA = False

logger = setup_logger("OrderReconciler", log_file="logs/order_reconciler.log")

# ── IST Timezone ──────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))


# ── Leaky Bucket Rate Limiter ─────────────────────────────────────────────────

class APIBucket:
    """Leaky-bucket rate limiter for Shoonya API (200 req/min).
    
    Prevents rate-limit lockouts during high-volatility gamma flips
    where 30–40 orders may fire in seconds.
    """
    __slots__ = ("tokens", "max_tokens", "refill_rate", "last_refill")

    def __init__(self, max_tokens: int = 180, refill_per_sec: float = 200 / 60):
        self.tokens = float(max_tokens)
        self.max_tokens = max_tokens
        self.refill_rate = refill_per_sec  # ~3.33 tokens/sec
        self.last_refill = time.time()

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def can_consume(self, n: int = 1) -> bool:
        """Check if we have enough API credits without consuming."""
        self._refill()
        return self.tokens >= n

    def consume(self, n: int = 1) -> bool:
        """Attempt to consume n tokens. Returns False if insufficient."""
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    async def wait_and_consume(self, n: int = 1):
        """Block until tokens are available, then consume."""
        while not self.consume(n):
            await asyncio.sleep(0.1)


# ── Order Reconciler ──────────────────────────────────────────────────────────

class OrderReconciler:
    def __init__(self):
        self.mq = MQManager()
        from core.auth import get_redis_url
        redis_url = get_redis_url()
        self.r = redis.from_url(redis_url, decode_responses=True)

        # In-flight baskets: {parent_uuid: {start_time, legs: []}}
        self.inflight_baskets: dict[str, dict] = {}
        # Rollback retry tracking: {parent_uuid: retry_count}
        self.rollback_retries: dict[str, int] = {}

        # Memory-Only Fallback for Storage Exhaustion
        self._memory_pending_orders: dict[str, str] = {}
        self._storage_exhausted = False

        # Shoonya API + Rate Limiter
        self.api = None
        self.api_bucket = APIBucket()
        if _HAS_SHOONYA:
            try:
                host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
                ws_host = host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")
                self.api = NorenApi(host=host, websocket=ws_host)
            except Exception as e:
                logger.error(f"Failed to init Shoonya API: {e}")

        self.margin_manager = AsyncMarginManager(self.r)

        # Cached broker orderbook (refreshed per reconciliation cycle)
        self._orderbook_cache: list[dict] | None = None
        self._orderbook_cache_ts = 0.0

        # MQ Sockets
        self.cmd_pub = None
        self.order_push = None
        self.hb = HeartbeatProvider("OrderReconciler", self.r)

    # ── Time Utilities ────────────────────────────────────────────────────

    def _is_terminal_hour(self) -> bool:
        """14:30–15:30 IST = Zero-Trust Hour."""
        now = datetime.now(IST)
        minutes = now.hour * 60 + now.minute
        return 870 <= minutes <= 930  # 14:30=870, 15:30=930

    def _reconcile_timeout(self) -> float:
        """Adaptive timeout: 100ms in Terminal Hour, 500ms normal."""
        return 0.1 if self._is_terminal_hour() else 0.5

    # ── Watchdog Loop ─────────────────────────────────────────────────────

    async def _watchdog_loop(self):
        """Monitors in-flight baskets at 100ms resolution."""
        while True:
            try:
                now = time.time()
                timeout = self._reconcile_timeout()
                to_reconcile = []

                for p_uuid, data in list(self.inflight_baskets.items()):
                    age = now - data["start_time"]
                    if age > timeout:
                        to_reconcile.append(p_uuid)

                if to_reconcile:
                    # Invalidate orderbook cache for fresh batch fetch
                    self._orderbook_cache = None

                for p_uuid in to_reconcile:
                    await self.reconcile_basket(p_uuid)

                await asyncio.sleep(0.1)  # 100ms watchdog resolution
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                await asyncio.sleep(0.5)

    # ── Basket Reconciliation ─────────────────────────────────────────────

    async def reconcile_basket(self, p_uuid: str):
        """Shield reconciliation from SIGTERM cancellation."""
        await asyncio.shield(self._reconcile_logic(p_uuid))

    async def _reconcile_logic(self, p_uuid: str):
        """Broken Condor Gap Reconciliation.

        Key changes:
          - Atomic Kill-Switch: FILLED+REJECTED → immediate Market Exit
          - Batch orderbook fetch instead of per-order polling
          - Redis Hash reads instead of JSON parsing
        """
        data = self.inflight_baskets.pop(p_uuid, None)
        if not data:
            return

        logger.info(f"🔍 Reconciling basket: {p_uuid} (timeout={self._reconcile_timeout():.0f}ms)")

        all_filled = True
        needs_force_fill: list[dict] = []
        filled_legs: list[dict] = []
        has_rejection = False

        for order in data["legs"]:
            order_id = order.get("order_id")
            if not order_id:
                continue

            status = await self._get_order_status(order_id, p_uuid)
            st = status.get("status", "PENDING")

            if st == "COMPLETE":
                filled_legs.append(order)
                continue
            elif st == "PARTIAL_FILL":
                filled_legs.append(order)
                try:
                    remaining = float(status.get("remaining_qty", 0))
                except (TypeError, ValueError):
                    remaining = 0.0
                if remaining > 0:
                    needs_force_fill.append({**order, "qty": remaining})
                all_filled = False
            elif st in ("REJECTED", "CANCELLED"):
                has_rejection = True
                all_filled = False
            else:
                all_filled = False
                needs_force_fill.append(order)

        # ── ATOMIC KILL-SWITCH ────────────────────────────────────────
        # If ANY leg is REJECTED and ANY other leg is FILLED/PARTIAL,
        # immediately dispatch Market Exit for filled legs. No waiting.
        if has_rejection and filled_legs:
            logger.critical(
                f"🚨 ATOMIC KILL-SWITCH: Basket {p_uuid} has REJECTED + FILLED legs. "
                f"Dispatching immediate Market Exit for {len(filled_legs)} filled legs."
            )
            await self._log_reconciliation_event(p_uuid, "ATOMIC_KILL_SWITCH")
            await self.trigger_rollback(p_uuid, data["legs"])
            return

        # ── Standard reconciliation ───────────────────────────────────
        if needs_force_fill or has_rejection:
            await self._log_reconciliation_event(
                p_uuid, "ROLLBACK" if has_rejection else "FORCE_FILL"
            )

        if has_rejection:
            await self.trigger_rollback(p_uuid, data["legs"])
        elif needs_force_fill:
            # Wing-First Priority: secure protective BUY legs first
            needs_force_fill.sort(
                key=lambda x: 0 if str(x.get("action", x.get("side", ""))).upper() == "BUY" else 1
            )
            await self.force_fill(p_uuid, needs_force_fill)
        elif all_filled:
            logger.info(f"✅ Basket {p_uuid} reconciled: FULLY FILLED")

    # ── Order Status Resolution ───────────────────────────────────────

    async def _get_order_status(self, order_id: str, parent_uuid: str = "") -> dict:
        """Resolve order status via Redis Hash → Broker Batch Fetch fallback.
        
        Returns dict with at minimum {"status": "..."}.
        """
        # 1. Redis Hash lookup (Change 4: Hash, not JSON)
        try:
            status = await self.r.hgetall(f"order_status:{order_id}")
            if status and "status" in status:
                return status
        except Exception as e:
            logger.error(f"Redis Hash read failed for {order_id}: {e}")
            self._storage_exhausted = True

        # 2. Batch Broker inquiry (Change 2: single orderbook fetch)
        if self.api and self.api_bucket.can_consume():
            entry = await self._find_in_orderbook(order_id, parent_uuid)
            if entry:
                return entry

        return {"status": "PENDING"}

    async def _find_in_orderbook(self, order_id: str, parent_uuid: str = "") -> dict | None:
        """Search the cached/fetched orderbook for an order.
        
        Uses batch `get_orderbook` (1 API call) instead of per-order
        `get_singleorderhistory` (N calls). Cache valid for 2s.
        """
        # Refresh cache if stale
        if not self._orderbook_cache or time.time() - self._orderbook_cache_ts > 2.0:
            if not self.api_bucket.consume():
                logger.warning("⏳ API bucket empty. Skipping broker inquiry.")
                return None

            try:
                loop = asyncio.get_running_loop()
                book = await loop.run_in_executor(None, self.api.get_order_book)
                self._orderbook_cache = book if isinstance(book, list) else []
                self._orderbook_cache_ts = time.time()
                logger.info(f"📒 Orderbook fetched: {len(self._orderbook_cache)} entries")
            except Exception as e:
                logger.error(f"Orderbook fetch failed: {e}")
                return None

        # Search by remarks (Change 3: O(1) routing via K7:{parent_uuid}:{order_id})
        for entry in self._orderbook_cache:
            remarks = entry.get("remarks", "")
            # Match by our injected remarks format or by broker order number
            if (f":{order_id}" in remarks or
                entry.get("norenordno") == order_id or
                (parent_uuid and f":{parent_uuid}:" in remarks)):

                b_status = (entry.get("status") or "").upper()
                if b_status == "COMPLETE":
                    mapped = "COMPLETE"
                elif b_status in ("REJECTED", "CANCELLED"):
                    mapped = "REJECTED"
                elif "PARTIAL" in b_status:
                    mapped = "PARTIAL_FILL"
                else:
                    mapped = "PENDING"

                return {
                    "status": mapped,
                    "filled_qty": str(entry.get("fillshares", 0)),
                    "remaining_qty": str(
                        float(entry.get("qty", 0)) - float(entry.get("fillshares", 0))
                    ),
                    "broker_orderno": entry.get("norenordno", ""),
                }

        return None

    # ── Force Fill ────────────────────────────────────────────────────

    async def force_fill(self, p_uuid: str, legs: list):
        """Force-fill remaining legs via Market Orders."""
        logger.warning(f"⚠️ Partial fill for {p_uuid}. Forcing {len(legs)} legs via Market Orders.")
        for leg in legs:
            order_cmd = {
                "order_id": f"RECON_{int(time.time())}_{p_uuid[:8]}",
                "parent_uuid": p_uuid,
                "symbol": leg["symbol"],
                "action": leg.get("action", leg.get("side")),
                "quantity": float(leg.get("quantity", leg.get("qty", 1))),
                "order_type": "MARKET",
                "strategy_id": "RECONCILER",
                "execution_type": leg.get("execution_type", "Paper"),
                "reason": "RECONCILIATION_FORCE_FILL",
            }
            await self.mq.send_json(self.order_push, Topics.ORDER_INTENT, order_cmd)

    # ── Rollback ──────────────────────────────────────────────────────

    async def trigger_rollback(self, p_uuid: str, all_legs: list):
        """Circuit Breaker Rollback with dead-letter escalation after 3 attempts."""
        retry_count = self.rollback_retries.get(p_uuid, 0) + 1
        self.rollback_retries[p_uuid] = retry_count

        if retry_count > 3:
            logger.critical(
                f"💀 DEAD-LETTER: Basket {p_uuid} rollback failed {retry_count}x. "
                f"Moving to CRITICAL_INTERVENTION."
            )
            await self.r.rpush("CRITICAL_INTERVENTION", json.dumps({
                "parent_uuid": p_uuid,
                "legs": [{k: v for k, v in l.items() if isinstance(v, (str, int, float))} for l in all_legs],
                "retry_count": retry_count,
                "timestamp": time.time(),
                "reason": "ROLLBACK_EXHAUSTED",
            }))
            from core.alerts import send_cloud_alert
            asyncio.create_task(send_cloud_alert(
                f"💀 DEAD LETTER: Basket {p_uuid} rollback failed {retry_count}x.\n"
                f"MANUAL INTERVENTION REQUIRED!\n"
                f"Check CRITICAL_INTERVENTION queue in Redis.",
                alert_type="CRITICAL",
            ))
            self.rollback_retries.pop(p_uuid, None)
            return

        logger.error(f"🚨 Basket {p_uuid} REJECTION. Rollback attempt {retry_count}/3.")

        # 1. Global rollback command
        cmd = {
            "cmd": "BASKET_ROLLBACK",
            "parent_uuid": p_uuid,
            "reason": "CIRCUIT_BREAKER_OR_REJECTION",
        }
        await self.mq.send_json(self.cmd_pub, "GLOBAL", cmd)

        # 2. Close executed legs (FILLED or PARTIAL_FILL)
        for leg in all_legs:
            order_id = leg.get("order_id")
            if not order_id:
                continue

            status = await self._get_order_status(order_id, p_uuid)
            st = status.get("status", "")

            # Release margin for rejected/cancelled legs
            if st in ("REJECTED", "CANCELLED"):
                orig_margin = float(leg.get("reserved_margin", 0))
                if orig_margin > 0:
                    await self.margin_manager.release(
                        orig_margin, execution_type=leg.get("execution_type", "Paper")
                    )
                    logger.info(f"💰 Released ₹{orig_margin:,.0f} for {leg.get('symbol')}")

            # Immediately close filled legs (naked risk)
            if st in ("COMPLETE", "PARTIAL_FILL"):
                qty = float(status.get("filled_qty", 0))
                if qty > 0:
                    logger.warning(f"🔄 Rolling back {leg.get('symbol')} ({qty} lots)")
                    close_order = {
                        "order_id": f"ROLL_{int(time.time())}_{p_uuid[:8]}",
                        "parent_uuid": p_uuid,
                        "symbol": leg["symbol"],
                        "action": "SELL" if leg.get("action", leg.get("side")) == "BUY" else "BUY",
                        "quantity": qty,
                        "order_type": "MARKET",
                        "strategy_id": "RECONCILER_ROLLBACK",
                        "execution_type": leg.get("execution_type", "Paper"),
                        "reason": "RECONCILIATION_ROLLBACK_CLOSE",
                    }
                    await self.mq.send_json(self.order_push, Topics.ORDER_INTENT, close_order)

    # ── Utilities ─────────────────────────────────────────────────────

    async def _log_reconciliation_event(self, p_uuid: str, action: str):
        """Log reconciliation event for counterfactual parity analysis."""
        await self.r.lpush("RECONCILIATION_LOG", json.dumps({
            "time": time.time(),
            "parent_uuid": p_uuid,
            "action": action,
            "reason": "Execution_Drift_Detected",
            "terminal_hour": self._is_terminal_hour(),
        }))

    # ── Reboot Audit ──────────────────────────────────────────────────

    async def _reboot_audit(self):
        """Triple-Sync on Startup: Broker ↔ DB ↔ Redis.
        
        Scans pending_orders hash for orphans using Redis Hash status reads.
        """
        logger.info("🔍 Reboot Audit: Scanning pending orders for orphans...")
        try:
            try:
                pending = await self.r.hgetall("pending_orders")
            except Exception as e:
                logger.error(f"Redis hgetall failed on reboot: {e}")
                self._storage_exhausted = True
                pending = self._memory_pending_orders

            if not pending:
                logger.info("✅ Reboot Audit: No pending orders. Clean state.")
                return

            resolved = 0
            phantoms = 0
            for order_id, data_raw in pending.items():
                try:
                    data = json.loads(data_raw)
                    dispatch_ts = data.get("dispatch_time_epoch", 0)
                    age_s = time.time() - dispatch_ts

                    # Check via Hash-based status
                    status = await self._get_order_status(order_id)

                    st = status.get("status", "")
                    if st in ("COMPLETE", "REJECTED", "CANCELLED"):
                        await self.r.hdel("pending_orders", order_id)
                        resolved += 1
                    elif age_s > 300:  # 5 min with no status = phantom
                        phantoms += 1
                        strat_id = data.get("strategy_id", "UNKNOWN")
                        await self.r.set(
                            f"STRATEGY_LOCK:{strat_id}",
                            "LOCKED_BY_RECONCILER_PHANTOM",
                        )
                        from core.alerts import send_cloud_alert
                        asyncio.create_task(send_cloud_alert(
                            f"👻 PHANTOM ORDER: {order_id}\n"
                            f"Strategy {strat_id} LOCKED.\n"
                            f"Symbol: {data.get('symbol')}\n"
                            f"Age: {age_s:.0f}s\n"
                            f"MANUAL VERIFICATION REQUIRED!",
                            alert_type="CRITICAL",
                        ))
                except Exception as e:
                    logger.error(f"Reboot audit error for {order_id}: {e}")

            if self._storage_exhausted:
                logger.warning("📉 Running in MEMORY-ONLY MODE (storage exhaustion).")

            logger.info(f"✅ Reboot Audit: {resolved} resolved, {phantoms} phantoms.")
        except Exception as e:
            logger.error(f"Reboot audit failed: {e}")

    # ── Main Run Loop ─────────────────────────────────────────────────

    async def run(self):
        logger.info("Order Reconciler active. (v2.0 — Sub-500ms Leg-Sync + Batch Ghost Hunter)")
        self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)
        self.order_push = self.mq.create_push(Ports.ORDERS, bind=False)

        await self._reboot_audit()

        sub = self.mq.create_subscriber(
            Ports.SYSTEM_CMD, topics=["BASKET_ORIGINATION"], bind=False
        )

        asyncio.create_task(self._watchdog_loop())

        if self.hb:
            asyncio.create_task(self.hb.run_heartbeat())

        while True:
            try:
                topic, msg = await self.mq.recv_json(sub)
                if not msg:
                    continue

                p_uuid = msg.get("parent_uuid")
                if p_uuid:
                    self.inflight_baskets[p_uuid] = {
                        "start_time": time.time(),
                        "legs": msg.get("legs", []),
                        "asset": msg.get("asset"),
                    }

                    if self._storage_exhausted:
                        for leg in msg.get("legs", []):
                            oid = leg.get("order_id")
                            if oid:
                                self._memory_pending_orders[oid] = json.dumps(leg)

                    logger.info(f"📥 Tracking basket: {p_uuid} ({len(msg.get('legs', []))} legs)")
            except zmq.Again:
                await asyncio.sleep(0.01)
                continue
            except Exception as e:
                logger.error(f"Recv error: {e}")
                await asyncio.sleep(1)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    reconciler = OrderReconciler()
    asyncio.run(reconciler.run())
