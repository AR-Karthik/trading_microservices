import asyncio
import json
import logging
import uuid
import time
from datetime import datetime, timezone
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("LiquidationDaemon")

class LiquidationDaemon:
    def __init__(self):
        self.mq = MQManager()
        self.orphaned_positions = {} # {symbol: {qty, entry_price, type, entry_time}}
        self.order_pub = self.mq.create_push(Ports.ORDERS)

    async def handle_handoffs(self):
        sub = self.mq.create_subscriber(Ports.SYSTEM_CMD, topics=["HANDOFF", "ORPHAN"])
        while True:
            _, msg = await self.mq.recv_json(sub)
            if msg:
                symbol = msg['symbol']
                msg['entry_time'] = time.time()
                self.orphaned_positions[symbol] = msg
                logger.info(f"Accepted Handoff/ORPHAN: {symbol} Qty={msg['quantity']}")
            await asyncio.sleep(0.1)

    async def monitor_and_exit(self, symbol, tick, state):
        if symbol not in self.orphaned_positions: return
        
        pos = self.orphaned_positions[symbol]
        price = tick['price']
        elapsed = time.time() - pos['entry_time']
        
        # 1. Price Barrier: ATR-mapped dynamic SL/TP (Mocked ATR=20)
        atr = 20
        tp_dist = atr * 2
        sl_dist = atr * 1
        
        exit_triggered = False
        reason = ""
        
        if pos['action'] == "BUY":
            if price >= pos['price'] + tp_dist: 
                exit_triggered = True; reason = "TP reached"
            elif price <= pos['price'] - sl_dist:
                exit_triggered = True; reason = "SL reached"
        elif pos['action'] == "SELL":
            if price <= pos['price'] - tp_dist:
                exit_triggered = True; reason = "TP reached"
            elif price >= pos['price'] + sl_dist:
                exit_triggered = True; reason = "SL reached"
                
        # 2. Time Barrier: 3-5 min Optimal Stopping
        if not exit_triggered and elapsed > 300: # 5 mins
            exit_triggered = True; reason = "Time Barrier"
            
        # 3. Signal Barrier: CVD reversal (Mocked)
        if not exit_triggered and state.get('s_total', 0) * (1 if pos['action']=="BUY" else -1) < -50:
            exit_triggered = True; reason = "Signal Reversal"
            
        if exit_triggered:
            order = {
                "order_id": str(uuid.uuid4()),
                "symbol": symbol,
                "action": "SELL" if pos['action'] == "BUY" else "BUY",
                "quantity": abs(pos['quantity']),
                "order_type": "MARKET",
                "strategy_id": "LIQUIDATION",
                "execution_type": pos.get('execution_type', 'Paper'),
                "reason": reason
            }
            await self.mq.send_json(self.order_pub, order, topic=Topics.ORDER_INTENT)
            logger.info(f"LIQUIDATION: Exited {symbol} | {reason} | Price: {price}")
            del self.orphaned_positions[symbol]

    async def run(self):
        logger.info("Liquidation Daemon Active.")
        asyncio.create_task(self.handle_handoffs())
        
        market_sub = self.mq.create_subscriber(Ports.MARKET_DATA, topics=[Topics.TICK_DATA])
        state_sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=[Topics.MARKET_STATE])
        
        latest_state = {}
        
        async def state_poller():
            nonlocal latest_state
            while True:
                _, state = await self.mq.recv_json(state_sub)
                if state: latest_state = state
                await asyncio.sleep(0.1)
        
        asyncio.create_task(state_poller())
        
        while True:
            try:
                topic, tick = await self.mq.recv_json(market_sub)
                if tick:
                    symbol = tick.get("symbol")
                    await self.monitor_and_exit(symbol, tick, latest_state)
            except Exception as e:
                logger.error(f"Error in LiquidationDaemon: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    daemon = LiquidationDaemon()
    asyncio.run(daemon.run())
