import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("LiquidationDaemon")

class LiquidationDaemon:
    """
    Handles "Orphaned" positions from strategies.
    Implements Optimal Stopping logic (EV tracking + ATR Trailing Stop).
    """
    def __init__(self):
        self.mq = MQManager()
        self.orphaned_positions = {} # {symbol: {qty, entry_price, type}}
        self.order_pub = self.mq.create_push(Ports.ORDERS)

    async def handle_handoffs(self):
        """Listens for handoff commands from strategies."""
        # This could be a specialized SUB socket for "HANDOFF"
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["HANDOFF"])
        while True:
            _, msg = await self.mq.recv_json(sub)
            if msg:
                symbol = msg['symbol']
                self.orphaned_positions[symbol] = msg
                logger.info(f"Accepted Handoff: {symbol} Qty={msg['quantity']}")
            await asyncio.sleep(0.1)

    async def monitor_and_exit(self, symbol, tick):
        if symbol not in self.orphaned_positions: return
        
        pos = self.orphaned_positions[symbol]
        price = tick['price']
        
        # Optimal Stopping Logic (Simplified)
        # 1. EV Check: If price drops below entry - ATR (Mocked)
        # 2. CVD Check: If tick volume is supporting the move
        
        exit_triggered = False
        # Placeholder for ATR/Volatility adjusted logic
        if pos['action'] == "BUY" and price < pos['price'] * 0.995:
            exit_triggered = True
        elif pos['action'] == "SELL" and price > pos['price'] * 1.005:
            exit_triggered = True
            
        if exit_triggered:
            order = {
                "order_id": str(uuid.uuid4()),
                "symbol": symbol,
                "action": "SELL" if pos['action'] == "BUY" else "BUY",
                "quantity": abs(pos['quantity']),
                "order_type": "MARKET",
                "strategy_id": "LIQUIDATION",
                "execution_type": pos.get('execution_type', 'Paper')
            }
            await self.mq.send_json(self.order_pub, order, topic=Topics.ORDER_INTENT)
            logger.info(f"LIQUIDATION: Exited {symbol} at {price}")
            del self.orphaned_positions[symbol]

    async def run(self):
        logger.info("Liquidation Daemon Active.")
        asyncio.create_task(self.handle_handoffs())
        
        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA])
        while True:
            try:
                topic, tick = await self.mq.recv_json(market_sub)
                if tick:
                    symbol = tick.get("symbol")
                    await self.monitor_and_exit(symbol, tick)
            except Exception as e:
                logger.error(f"Error in LiquidationDaemon: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    daemon = LiquidationDaemon()
    asyncio.run(daemon.run())
