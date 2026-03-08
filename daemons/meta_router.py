import asyncio
import json
import logging
import redis
from datetime import datetime
from core.mq import MQManager, Ports, Topics

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("MetaRouter")

class MetaRouter:
    def __init__(self, test_mode=False):
        self.mq = MQManager()
        self.test_mode = test_mode
        if not test_mode:
            self.cmd_pub = self.mq.create_publisher(Ports.SYSTEM_CMD)
            self.redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        self.current_regime = None
        self.orphaned_at_lunch = False
        self.orphaned_at_eod = False

    def check_macro_windows(self):
        """Implement strict Entry/Exit Macro Windows."""
        now = datetime.now()
        current_time = now.strftime("%H:%M")
        
        # Entry Hard-Stop: 09:30-11:30 and 13:30-15:00
        is_entry_allowed = ("09:30" <= current_time <= "11:30") or ("13:30" <= current_time <= "15:00")
        
        # Exit Handoff: Exactly at 11:30 or 15:00
        should_orphan = (current_time == "11:30" and not self.orphaned_at_lunch) or \
                        (current_time == "15:00" and not self.orphaned_at_eod)
        
        if current_time == "11:30": self.orphaned_at_lunch = True
        if current_time == "15:00": self.orphaned_at_eod = True
        if current_time == "12:00": self.orphaned_at_lunch = False # Reset
        if current_time == "16:00": self.orphaned_at_eod = False # Reset
        
        return is_entry_allowed, should_orphan

    async def broadcast(self, score, should_orphan=False):
        commands = []
        if should_orphan:
            logger.warning("MACRO BOUNDARY REACHED: Issuing Global ORPHAN.")
            commands = [
                {"target": "STRAT_GAMMA", "command": "ORPHAN"},
                {"target": "STRAT_REVERSION", "command": "ORPHAN"},
                {"target": "STRAT_EXPIRY", "command": "ORPHAN"}
            ]
        else:
            # Alpha Scoring Matrix Mapping
            if score > 75:
                regime = "AGGR_LONG"
                commands = [{"target": "STRAT_GAMMA", "command": "ACTIVATE"}]
            elif score < -75:
                regime = "AGGR_SHORT"
                commands = [{"target": "STRAT_GAMMA", "command": "ACTIVATE"}]
            elif -39 <= score <= 39:
                regime = "SLEEP"
                commands = [{"target": "STRAT_GAMMA", "command": "ORPHAN"}]
            else:
                regime = "NEUTRAL"
        
        for cmd in commands:
            if not self.test_mode:
                await self.mq.send_json(self.cmd_pub, cmd, topic=cmd['target'])
        return commands

    async def run(self):
        logger.info("Meta Router Hierarchical Mode Active.")
        sub = self.mq.create_subscriber(Ports.MARKET_STATE, topics=[Topics.MARKET_STATE])
        
        while True:
            try:
                _, state = await self.mq.recv_json(sub)
                if state:
                    score = state.get("s_total", 0)
                    is_entry_allowed, should_orphan = self.check_macro_windows()
                    
                    await self.broadcast(score, should_orphan)
                    
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error in MetaRouter: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    router = MetaRouter()
    asyncio.run(router.run())
