import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("StratGamma")

class GammaScalper:
    """
    Regime: TREND / EXPIRY.
    Focus: Gamma acceleration in Nifty options.
    """
    def __init__(self, symbols=["NIFTY50"]):
        self.mq = MQManager()
        self.symbols = symbols
        self.state = "SLEEP" 
        self.positions = {} # {symbol: {qty, price, action}}
        self.order_pub = self.mq.create_publisher(Ports.ORDERS, bind=False)
        self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)

    async def handle_commands(self):
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["STRAT_GAMMA"])
        while True:
            _, cmd = await self.mq.recv_json(sub)
            if cmd:
                new_state = cmd.get("command")
                if new_state != self.state:
                    logger.info(f"State Transition: {self.state} -> {new_state}")
                    if new_state == "ORPHAN" and self.positions:
                        await self.handoff_positions()
                    self.state = new_state
            await asyncio.sleep(0.1)

    async def handoff_positions(self):
        """Handoff orphaned positions to Liquidation Daemon."""
        for symbol, pos in list(self.positions.items()):
            logger.warning(f"ORPHANING: Handing off {symbol} to Liquidation Daemon")
            await self.mq.send_json(self.cmd_pub, pos, topic="HANDOFF")
            del self.positions[symbol]

    async def on_tick(self, symbol, tick):
        if self.state != "ACTIVATE":
            return
            
        price = tick['price']
        if symbol not in self.positions:
            # Prototype Entry: Buy if price > threshold (mock gamma burst)
            order = {
                "order_id": str(uuid.uuid4()),
                "symbol": symbol,
                "action": "BUY",
                "quantity": 50,
                "order_type": "MARKET",
                "price": price,
                "strategy_id": "STRAT_GAMMA",
                "execution_type": "Paper"
            }
            await self.mq.send_json(self.order_pub, order, topic=Topics.ORDER_INTENT)
            self.positions[symbol] = {"symbol": symbol, "quantity": 50, "price": price, "action": "BUY"}
            logger.info(f"GAMMA: Entry at {price}")

    async def run(self):
        logger.info("Gamma Scalper Active.")
        asyncio.create_task(self.handle_commands())
        
        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA])
        while True:
            try:
                topic, tick = await self.mq.recv_json(market_sub)
                if tick:
                    symbol = tick.get("symbol")
                    if symbol in self.symbols:
                        await self.on_tick(symbol, tick)
            except Exception as e:
                logger.error(f"Error in StratGamma: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    strat = GammaScalper()
    asyncio.run(strat.run())
