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
    def __init__(self, execution_engine):
        self.engine = execution_engine

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
        parent_uuid = orders[0].get("parent_uuid")
        if parent_uuid and self.mq:
            from core.mq import Ports
            # Reuse existing MQ context/manager instead of creating new one per call [Audit 2.5]
            cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD, bind=False)
            ping = {
                "parent_uuid": parent_uuid,
                "legs": [{"symbol": o["symbol"], "side": o.get("side", o.get("action")), "qty": o.get("qty", o.get("quantity"))} for o in sorted_orders],
                "asset": orders[0].get("asset", "GLOBAL")
            }
            await self.mq.send_json(cmd_pub, "BASKET_ORIGINATION", ping)
            cmd_pub.close()

        logger.info(f"⚡ Executing {len(sorted_orders)} legs {'sequentially' if sequential else 'atomically'}...")
        
        results = []
        for order in sorted_orders:
            try:
                # Use adaptive chasing for deep OTM wings
                if order.get("is_otm", False):
                    res = await self.adaptive_limit_chase(order)
                else:
                    res = await self.engine.place_order(order)
                results.append(res)
            except Exception as e:
                logger.error(f"Leg execution failed: {e}")
                results.append(e)
                # Rollback logic is handled by OrderReconciler upon timeout or failure
                raise e 

        return results
