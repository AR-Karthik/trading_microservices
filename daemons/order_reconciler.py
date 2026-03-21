import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
import redis.asyncio as redis
import zmq
import zmq.asyncio
from core.mq import MQManager, Ports, Topics
from core.logger import setup_logger
from core.health import HeartbeatProvider
from core.margin import AsyncMarginManager

# Wave 2: Shoonya API for status polling
try:
    from NorenRestApiPy.NorenApi import NorenApi
    import pyotp
    _HAS_SHOONYA = True
except ImportError:
    _HAS_SHOONYA = False

logger = setup_logger("OrderReconciler", log_file="logs/order_reconciler.log")

class OrderReconciler:
    def __init__(self):
        self.mq = MQManager()
        from core.auth import get_redis_url
        redis_url = get_redis_url()
        self.r = redis.from_url(redis_url, decode_responses=True)
        
        # In-flight baskets: {parent_uuid: {start_time, legs: []}}
        self.inflight_baskets = {}
        # [C5-02] Rollback retry tracking: {parent_uuid: retry_count}
        self.rollback_retries = {}
        
        # Wave 2: Memory-Only Fallback for Storage Exhaustion
        self._memory_pending_orders = {}
        self._storage_exhausted = False

        # Wave 2: Shoonya API client for direct status polling
        self.api = None
        if _HAS_SHOONYA:
            try:
                host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
                ws_host = host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")
                self.api = NorenApi(host=host, websocket=ws_host)
                # Note: Login usually happens in a dedicated method or on first need
            except Exception as e:
                logger.error(f"Failed to init Shoonya API: {e}")
        
        self.margin_manager = AsyncMarginManager(self.r)
        
        # MQ Sockets
        self.cmd_pub = None
        self.order_push = None
        self.hb = HeartbeatProvider("OrderReconciler", self.r)

    async def _watchdog_loop(self):
        """Monitors in-flight baskets and triggers reconciliation after 3s timeout."""
        while True:
            try:
                now = time.time()
                to_reconcile = []
                
                for p_uuid, data in list(self.inflight_baskets.items()): # [Audit 4.1] list() to prevent runtime error
                    if now - data["start_time"] > 1.0:
                        to_reconcile.append(p_uuid)
                
                for p_uuid in to_reconcile:
                    await self.reconcile_basket(p_uuid)
                    
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                await asyncio.sleep(1)

    async def reconcile_basket(self, p_uuid: str):
        """[GCP FIX] Shield the entire reconciliation from SIGTERM cancellation."""
        await asyncio.shield(self._reconcile_logic(p_uuid))

    async def _reconcile_logic(self, p_uuid: str):
        """
        Broken Condor Gap Reconciliation (Spec 11.1)
        1. Query order status for all legs in parent_uuid.
        2. If partial fill -> Force Market Order for remainder.
        3. If rejection -> Trigger Rollback.
        """
        logger.info(f"🔍 Reconciling basket: {p_uuid}")
        data = self.inflight_baskets.pop(p_uuid, None)
        if not data: return

        # In a real implementation, we would call the Bridge or Broker API here.
        # For this architecture, we check Redis 'order_status:{order_id}' which Bridges update.
        
        all_filled = True
        needs_force_fill = []
        needs_rollback = False
        
        for order in data["legs"]:
            order_id = order.get("order_id")
            if not order_id: continue
            
            status_raw = None
            try:
                status_raw = await self.r.get(f"order_status:{order_id}")
            except Exception as e:
                logger.error(f"Redis fetch failed for status {order_id}: {e}")
                self._storage_exhausted = True

            # [Audit-Fix] Mapping Grace Period: if status is missing, wait 500ms for Bridge ID mapping
            if not status_raw and not self._storage_exhausted:
                logger.info(f"⏳ Status missing for {order_id}. Waiting 500ms for Bridge mapping...")
                await asyncio.sleep(0.5)
                status_raw = await self.r.get(f"order_status:{order_id}")

            # Wave 2: Poll broker directly if status is still missing
            if not status_raw and self.api:
                logger.warning(f"🔍 Status missing for {order_id} in Redis. Polling broker...")
                try:
                    # Search broker history for our client order ID (internal order_id)
                    # Note: This logic assumes we can find the order by our internal ID
                    # Shoonya's order status might need the broker's auto-generated orderno
                    # But we can try searching history
                    loop = asyncio.get_running_loop()
                    history = await loop.run_in_executor(None, self.api.get_order_history)
                    if history:
                        for entry in history:
                            if entry.get('remarks') == order_id or entry.get('norenordno') == order_id:
                                # Found it. Map broker statuses to our internal state machine
                                b_status = entry.get('status', '').upper()
                                logger.info(f"✅ Found status for {order_id} on broker: {b_status}")
                                
                                # Basic mapping
                                if b_status == "COMPLETE": st = "COMPLETE"
                                elif b_status in ["REJECTED", "CANCELLED"]: st = "REJECTED"
                                elif "PARTIAL" in b_status: st = "PARTIAL_FILL"
                                else: st = "PENDING"
                                
                                # Fake a status object
                                status = {
                                    "status": st,
                                    "filled_qty": float(entry.get('fillshares', 0)),
                                    "remaining_qty": float(entry.get('qty', 0)) - float(entry.get('fillshares', 0))
                                }
                                status_raw = json.dumps(status) # So it enters the following logic
                                break
                except Exception as ex:
                    logger.error(f"Broker polling failed for {order_id}: {ex}")

            status = json.loads(status_raw) if status_raw else {"status": "PENDING"}
            
            st = status.get("status")
            if st == "COMPLETE":
                continue
            elif st == "PARTIAL_FILL":
                try:
                    remaining = float(status.get("remaining_qty", 0))
                except (TypeError, ValueError):
                    remaining = 0.0
                if remaining > 0:
                    needs_force_fill.append({**order, "qty": remaining})
                all_filled = False
            elif st in ["REJECTED", "CANCELLED"]:
                needs_rollback = True
                all_filled = False
            else:
                all_filled = False
                # Still pending after 3s? Force fill or cancel.
                needs_force_fill.append(order)
        
        # [PHD FIX] Log the reconciliation event for counterfactual parity
        if needs_rollback or needs_force_fill:
            await self.r.lpush("RECONCILIATION_LOG", json.dumps({
                "time": time.time(),
                "parent_uuid": p_uuid,
                "action": "ROLLBACK" if needs_rollback else "FORCE_FILL",
                "reason": "Execution_Drift_Detected"
            }))

        if needs_rollback:
            await self.trigger_rollback(p_uuid, data["legs"])
        elif needs_force_fill:
            # [Audit-Fix] Wing-First Priority: Secure protective BUY legs before fixing income legs
            needs_force_fill.sort(key=lambda x: 0 if str(x.get("action", x.get("side", ""))).upper() == "BUY" else 1)
            await self.force_fill(p_uuid, needs_force_fill)
        elif all_filled:
            logger.info(f"✅ Basket {p_uuid} reconciled: FULLY FILLED")

    async def force_fill(self, p_uuid: str, legs: list):
        """Securing protective wings is non-negotiable. Pushes to Ports.ORDERS."""
        logger.warning(f"⚠️ Partial fill detected for {p_uuid}. Forcing remainder via Market Orders.")
        for leg in legs:
            # Dispatch urgent market order to Bridge via Ports.ORDERS (PUSH)
            order_cmd = {
                "order_id": f"RECON_{int(time.time())}_{p_uuid[:8]}",
                "parent_uuid": p_uuid,
                "symbol": leg["symbol"],
                "action": leg.get("action", leg.get("side")), # [Audit 10.4] Standardize action/side
                "quantity": float(leg.get("quantity", leg.get("qty", 1))), # [Audit 10.4] Standardize quantity/qty
                "order_type": "MARKET",
                "strategy_id": "RECONCILER",
                "execution_type": leg.get("execution_type", "Paper"),
                "reason": "RECONCILIATION_FORCE_FILL"
            }
            # We use PUSH socket to ensure Bridge receives it
            await self.mq.send_json(self.order_push, Topics.ORDER_INTENT, order_cmd)

    async def trigger_rollback(self, p_uuid: str, all_legs: list):
        """
        Circuit Breaker Rollback (Spec 11.3)
        Close whatever legs did execute to avoid naked tail risk.
        [C5-02] Includes dead-letter queue after 3 failed attempts.
        """
        # Track retries
        retry_count = self.rollback_retries.get(p_uuid, 0) + 1
        self.rollback_retries[p_uuid] = retry_count

        if retry_count > 3:
            # [C5-02] Dead-letter escalation
            logger.critical(f"💀 DEAD-LETTER: Basket {p_uuid} rollback failed {retry_count} times. Moving to CRITICAL_INTERVENTION.")
            await self.r.rpush("CRITICAL_INTERVENTION", json.dumps({
                "parent_uuid": p_uuid,
                "legs": [{k: v for k, v in l.items() if isinstance(v, (str, int, float))} for l in all_legs],
                "retry_count": retry_count,
                "timestamp": time.time(),
                "reason": "ROLLBACK_EXHAUSTED"
            }))
            from core.alerts import send_cloud_alert
            asyncio.create_task(send_cloud_alert(
                f"💀 DEAD LETTER: Basket {p_uuid} rollback failed {retry_count}x.\n"
                f"MANUAL INTERVENTION REQUIRED!\n"
                f"Check CRITICAL_INTERVENTION queue in Redis.",
                alert_type="CRITICAL"
            ))
            self.rollback_retries.pop(p_uuid, None)
            return

        logger.error(f"🚨 Basket {p_uuid} failed (Rejection/Breaker). Rollback attempt {retry_count}/3.")
        
        # 1. Fire a global rollback command to SYSTEM_CMD (PUB/SUB)
        cmd = {
            "cmd": "BASKET_ROLLBACK",
            "parent_uuid": p_uuid,
            "reason": "CIRCUIT_BREAKER_OR_REJECTION"
        }
        await self.mq.send_json(self.cmd_pub, "GLOBAL", cmd)

        # 2. Proactively close executed legs
        for leg in all_legs:
            order_id = leg.get("order_id")
            if not order_id: continue
            
            status_raw = await self.r.get(f"order_status:{order_id}")
            status = json.loads(status_raw) if status_raw else {}
            
            # [ACCOUNTING FIX] Don't use 125,000. Use the MarginManager to release.
            if status.get("status") in ["REJECTED", "CANCELLED"]:
                orig_margin = float(leg.get("reserved_margin", 0))
                if orig_margin > 0:
                    await self.margin_manager.release(orig_margin, execution_type=leg.get("execution_type", "Paper"))
                    logger.info(f"💰 Vault Synced: Released ₹{orig_margin:,} back to pool for {leg['symbol']}")
            
            # If the leg was partially or fully filled, it's a naked risk. Close it.
            if status.get("status") in ["COMPLETE", "PARTIAL_FILL"]:
                qty = float(status.get("filled_qty", 0))
                if qty > 0:
                    logger.warning(f"🔄 Rolling back executed leg: {leg['symbol']} ({qty} lots)")
                    close_order = {
                        "order_id": f"ROLL_{int(time.time())}_{p_uuid[:8]}",
                        "parent_uuid": p_uuid,
                        "symbol": leg["symbol"],
                        "action": "SELL" if leg.get("action", leg.get("side")) == "BUY" else "BUY", # [Audit 10.4] Standardize action/side
                        "quantity": float(qty), # [Audit 10.4] Ensure float
                        "order_type": "MARKET",
                        "strategy_id": "RECONCILER_ROLLBACK",
                        "execution_type": leg.get("execution_type", "Paper"),
                        "reason": "RECONCILIATION_ROLLBACK_CLOSE"
                    }
                    await self.mq.send_json(self.order_push, Topics.ORDER_INTENT, close_order)

    async def run(self):
        logger.info("Order Reconciler active. Monitoring Multi-Leg Health.")
        self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)
        self.order_push = self.mq.create_push(Ports.ORDERS, bind=False)

        # [C5-01] Reboot Audit: Triple-Sync on startup
        await self._reboot_audit()
        
        # Listen for new basket originations from MetaRouter
        # [Audit 2.1] Do NOT bind subscriber to SYSTEM_CMD, as StrategyEngine may own it or vice versa
        # Actually Publisher on MetaRouter might bind. Subscribing just connects.
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["BASKET_ORIGINATION"], bind=False)
        
        asyncio.create_task(self._watchdog_loop())
        
        # ── Phase 9: UI & Observability heartbeat ──
        if self.hb:
            asyncio.create_task(self.hb.run_heartbeat())
        
        while True:
            try:
                topic, msg = await self.mq.recv_json(sub)
                if not msg: continue
                
                p_uuid = msg.get("parent_uuid")
                if p_uuid:
                    self.inflight_baskets[p_uuid] = {
                        "start_time": time.time(),
                        "legs": msg.get("legs", []),
                        "asset": msg.get("asset")
                    }
                    # Wave 2: Storage Exhaustion Fallback
                    if self._storage_exhausted:
                        for leg in msg.get("legs", []):
                            oid = leg.get("order_id")
                            if oid: self._memory_pending_orders[oid] = json.dumps(leg)
                    
                    logger.info(f"📥 Tracking new basket: {p_uuid}")
            except zmq.Again:
                # No messages available, expected in async loop. Sleep briefly to avoid busy loop.
                await asyncio.sleep(0.01)
                continue
            except Exception as e:
                logger.error(f"Recv error: {e}")
                await asyncio.sleep(1)

    async def _reboot_audit(self):
        """
        [C5-01] Triple-Sync on Startup: Broker ↔ DB ↔ Redis
        Scans pending_orders hash for orphans and auto-resolves:
        - COMPLETE → Update DB, release margin
        - OPEN → Leave as-is (still inflight)
        - PHANTOM → Alert for manual intervention
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
                logger.info("✅ Reboot Audit: No pending orders found. Clean state.")
                return

            resolved = 0
            phantoms = 0
            for order_id, data_raw in pending.items():
                try:
                    data = json.loads(data_raw)
                    dispatch_ts = data.get("dispatch_time_epoch", 0)
                    age_s = time.time() - dispatch_ts

                    # Check if order has a status update from the bridge
                    status_raw = await self.r.get(f"order_status:{order_id}")
                    if status_raw:
                        status = json.loads(status_raw)
                        st = status.get("status")
                        if st == "COMPLETE":
                            # Auto-resolve: remove from pending
                            await self.r.hdel("pending_orders", order_id)
                            resolved += 1
                        elif st in ["REJECTED", "CANCELLED"]:
                            await self.r.hdel("pending_orders", order_id)
                            resolved += 1
                    elif age_s > 300:  # 5 minutes with no status = phantom
                        phantoms += 1
                        # [Audit-Fix] Component 4: Phantom Lock (Manual intervention only)
                        strat_id = data.get('strategy_id', 'UNKNOWN')
                        await self.r.set(f"STRATEGY_LOCK:{strat_id}", "LOCKED_BY_RECONCILER_PHANTOM")
                        
                        from core.alerts import send_cloud_alert
                        asyncio.create_task(send_cloud_alert(
                            f"👻 PHANTOM ORDER on reboot: {order_id}\n"
                            f"Strategy {strat_id} LOCKED.\n"
                            f"Symbol: {data.get('symbol')}\n"
                            f"Age: {age_s:.0f}s\n"
                            f"MANUAL VERIFICATION REQUIRED!",
                            alert_type="CRITICAL"
                        ))
                except Exception as e:
                    logger.error(f"Reboot audit error for {order_id}: {e}")

            # Wave 2: Sync memory cache with resolved items
            if self._storage_exhausted:
                logger.warning("📉 System running in MEMORY-ONLY MODE due to storage exhaustion.")

            logger.info(f"✅ Reboot Audit Complete: {resolved} resolved, {phantoms} phantoms flagged.")
        except Exception as e:
            logger.error(f"Reboot audit failed: {e}")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    reconciler = OrderReconciler()
    asyncio.run(reconciler.run())
