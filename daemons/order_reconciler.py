import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics
from core.logger import setup_logger
from core.health import HeartbeatProvider

logger = setup_logger("OrderReconciler", log_file="logs/order_reconciler.log")

class OrderReconciler:
    def __init__(self):
        self.mq = MQManager()
        redis_host = os.getenv("REDIS_HOST", "localhost")
        self.r = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)
        
        # In-flight baskets: {parent_uuid: {start_time, legs: []}}
        self.inflight_baskets = {}
        
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
                    if now - data["start_time"] > 3.0:
                        to_reconcile.append(p_uuid)
                
                for p_uuid in to_reconcile:
                    await self.reconcile_basket(p_uuid)
                    
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                await asyncio.sleep(1)

    async def reconcile_basket(self, p_uuid: str):
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
            
            status_raw = await self.r.get(f"order_status:{order_id}")
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

        if needs_rollback:
            await self.trigger_rollback(p_uuid, data["legs"])
        elif needs_force_fill:
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
        """
        logger.error(f"🚨 Basket {p_uuid} failed (Rejection/Breaker). Triggering Rollback Protocol.")
        
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
                    logger.info(f"📥 Tracking new basket: {p_uuid}")
            except Exception as e:
                logger.error(f"Recv error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    reconciler = OrderReconciler()
    asyncio.run(reconciler.run())
