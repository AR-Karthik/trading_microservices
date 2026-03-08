import asyncio
import json
import logging
import uuid
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("StratExpiry")

class ExpiryScalper:
    """
    Regime: EXPIRY / VOLATILE.
    Focus: Rapid scalp of ATM/OTM options on expiry days.
    """
    def __init__(self, symbols=["NIFTY50", "BANKNIFTY"]):
        self.mq = MQManager()
        self.symbols = symbols
        self.state = "SLEEP"
        self.positions = {}
        self.order_pub = self.mq.create_publisher(Ports.ORDERS, bind=False)
        self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)

    async def handle_commands(self):
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["STRAT_EXPIRY"])
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
        for symbol, pos in list(self.positions.items()):
            logger.warning(f"ORPHANING: Handing off {symbol}")
            await self.mq.send_json(self.cmd_pub, pos, topic="HANDOFF")
            del self.positions[symbol]

    async def on_tick(self, symbol, tick):
        if self.state != "ACTIVATE":
            return
            
        # Mock Expiry Entry: High speed scalping
        if symbol not in self.positions:
            order = {
                "order_id": str(uuid.uuid4()),
                "symbol": symbol,
                "action": "BUY",
                "quantity": 50,
                "order_type": "MARKET",
                "price": tick['price'],
                "strategy_id": "STRAT_EXPIRY",
                "execution_type": "Paper"
            }
            await self.mq.send_json(self.order_pub, order, topic=Topics.ORDER_INTENT)
            self.positions[symbol] = {"symbol": symbol, "quantity": 50, "price": tick['price'], "action": "BUY"}
            logger.info(f"EXPIRY: Scalp Entry at {tick['price']}")

    async def run(self):
        logger.info("Expiry Scalper Active.")
        asyncio.create_task(self.handle_commands())
        
        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA])
        while True:
            try:
                topic, tick = await self.mq.recv_json(market_sub)
                if tick:
                    await self.on_tick(tick.get("symbol"), tick)
            except Exception as e:
                logger.error(f"Error in StratExpiry: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    strat = ExpiryScalper()
    asyncio.run(strat.run())
