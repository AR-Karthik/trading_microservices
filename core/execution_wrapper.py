import asyncio
import logging
import uuid
from typing import List, Dict

logger = logging.getLogger(__name__)

class MultiLegExecutor:
    """
    Handles 'Atomic' execution of multiple orders simultaneously or sequentially.
    Useful for straddles, spreads, or hedging where legs must be placed as close
    together in time as possible, but with strict sequencing for margin safety.
    """
    # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
    def __init__(self, execution_engine, mq_manager=None, cmd_pub=None):
        self.engine = execution_engine
        self.mq = mq_manager
        self.cmd_pub = cmd_pub

    async def _get_best_price(self, symbol, side):
        # Fallback to current market price from engine/broker
        return await self.engine.get_last_price(symbol)

    async def adaptive_limit_chase(self, order, max_slippage_pct=0.10):
        """
        Chase the market with limit orders, capping slippage (Spec 11.2)
        """
        symbol = order["symbol"]
        qty = order["qty"]
        side = order["side"]
        
        best_price = await self._get_best_price(symbol, side)
        max_price = best_price * (1 + max_slippage_pct) if side == "BUY" else best_price * (1 - max_slippage_pct)
        current_limit = best_price

        for attempt in range(5):
            order["price"] = current_limit
            order["order_type"] = "LMT"
            
            order_id = await self.engine.place_order(order)
            await asyncio.sleep(0.5) 
            
            status = await self.engine.get_order_status(order_id)
            if status == "COMPLETE": 
                return order_id
            if status == "PARTIAL": 
                continue # Let it accumulate
            
            # Chase: modify limit worse by 1 tick (simplified here as 0.05)
            tick_size = 0.05
            current_limit = current_limit + tick_size if side == "BUY" else current_limit - tick_size
            
            if (side == "BUY" and current_limit > max_price) or (side == "SELL" and current_limit < max_price):
                await self.engine.cancel_order(order_id)
                logger.error(f"Slippage cap breached for {symbol}")
                # [Audit-Fix] Additive: Broadcast SLIPPAGE_HALT (60s veto)
                await self.engine.redis.set("SLIPPAGE_HALT", "True", ex=60)
                await self.engine.redis.publish("panic_channel", json.dumps({
                    "action": "SLIPPAGE_HALT",
                    "symbol": symbol,
                    "reason": "Slippage cap breached"
                }))
                raise Exception(f"SlippageCapBreached: {symbol}")
                
        raise Exception(f"FillTimeout: {symbol}")

    async def execute_legs(self, orders: List[Dict], sequential: bool = True):
        """
        Executes multiple orders with optional sequencing (Spec 11.4).
        Default: Sequential (Buy-First on Entry, Short-First on Exit).
        """
        if not orders:
            return []

        # ── Step 1: Sorting for Margin Safety ───────────────────────────────
        # Entry: BUY (Long) before SELL (Short)
        # Exit: BUY (Close Short) before SELL (Close Long)
        if sequential:
            # We sort by side: BUY (0) then SELL (1)
            # This covers both Entry (Buy Long first) and Exit (Buy to Close Short first)
            sorted_orders = sorted(orders, key=lambda x: 0 if x.get("side", x.get("action")) == "BUY" else 1)
        else:
            sorted_orders = orders

        # ── Step 2: Reconciler Ping (Spec 11.1) ──────────────────────────────
        # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
        parent_uuid = orders[0].get("parent_uuid")
        if parent_uuid and getattr(self, "cmd_pub", None) and getattr(self, "mq", None):
            ping = {
                "parent_uuid": parent_uuid,
                "legs": [{"symbol": o["symbol"], "side": o.get("side", o.get("action")), "qty": o.get("qty", o.get("quantity"))} for o in sorted_orders],
                "asset": orders[0].get("asset", "GLOBAL")
            }
            await self.mq.send_json(self.cmd_pub, "BASKET_ORIGINATION", ping)
            # Socket closed gracefully by caller lifecycle, avoiding FD leak

        # ── Step 3: JIT Feed Subscription (Spec 11.4) ────────────────────────
        for order in sorted_orders:
            exchange = getattr(self.engine, "_get_exchange", lambda x: "NFO")(order["symbol"])
            await self.engine.redis.publish("dynamic_subscriptions", f"{exchange}|{order['symbol']}")
        
        logger.info(f"⚡ Executing {len(sorted_orders)} legs {'sequentially' if sequential else 'atomically'}...")
        
        results = []
        for i, order in enumerate(sorted_orders):
            try:
                # Use adaptive chasing for deep OTM wings
                if order.get("is_otm", False):
                    res = await self.adaptive_limit_chase(order)
                else:
                    res = await self.engine.place_order(order)
                results.append(res)
                
                # Wait for Fill Confirmation if it's a BUY leg of a multi-leg basket (Spec 11.4)
                # This ensures margin is unlocked from the long leg before firing the short leg.
                action = order.get("side", order.get("action"))
                if sequential and action == "BUY" and i < len(sorted_orders) - 1:
                    logger.info(f"⏳ Awaiting fill confirmation for {order['symbol']} before next leg...")
                    # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
                    # We listen for 5 seconds max
                    pubsub = self.engine.redis.pubsub()
                    try:
                        await pubsub.subscribe("order_confirmations")
                        
                        confirmed = False
                        start_t = asyncio.get_event_loop().time()
                        while asyncio.get_event_loop().time() - start_t < 5.0:
                            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                            if msg:
                                conf = json.loads(msg['data'])
                                if conf.get("symbol") == order["symbol"] and conf.get("status") == "COMPLETE":
                                    confirmed = True
                                    break
                    finally:
                        await pubsub.unsubscribe("order_confirmations")
                        await pubsub.close()
                    if not confirmed:
                        logger.warning(f"⚠️ Fill confirmation timeout for {order['symbol']}. Proceeding anyway...")
                        
            except Exception as e:
                logger.error(f"Leg execution failed: {e}")
                results.append(e)
                raise e 

        return results
