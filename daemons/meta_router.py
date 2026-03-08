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
        
    def check_kill_switches(self, state):
        """Level 1: Global ORPHAN logic."""
        if state.get("spread_z", 0) > 3.0:
            return "HALT: Macro Shock (Spread Z-Score)"
        if state.get("book_depth", 1.0) < 0.5:
            return "HALT: Liquidity Vacuum"
        if state.get("rv", 0) < (state.get("iv", 1) / 100 / 252): # Very rough RV < IV check
            # This is complex, but the idea is to prevent theta-eating trades
            pass
        return None

    def apply_filters(self, state, regime):
        """Level 2: Strategic Filters."""
        # IF OFI_Z > 2.5 (Heavy Sweeping) -> Disable Mean-Reversion
        # IF Skew steeply positive -> Disable Long Calls
        pass

    def detect_regime(self, state):
        """Level 3: Math-based Dispatcher."""
        hurst = state.get("hurst", 0.5)
        # Mocking correlation for now
        correlation = 0.8 
        
        if hurst > 0.55 and correlation > 0.7:
            return "TREND"
        elif hurst < 0.45:
            return "CHOP"
        
        return "NEUTRAL"

    async def broadcast(self, regime, kill_reason=None):
        if regime != self.current_regime and not kill_reason:
            shift = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "old": self.current_regime,
                "new": regime
            }
            self.redis.lpush("regime_shifts", json.dumps(shift))
            self.redis.ltrim("regime_shifts", 0, 50)
            self.current_regime = regime
            
        commands = []
        if kill_reason:
            logger.warning(f"KILL-SWITCH TRIGGERED: {kill_reason}")
            commands = [
                {"target": "STRAT_GAMMA", "command": "ORPHAN"},
                {"target": "STRAT_REVERSION", "command": "ORPHAN"},
                {"target": "STRAT_EXPIRY", "command": "ORPHAN"}
            ]
        else:
            if regime == "TREND":
                commands = [{"target": "STRAT_GAMMA", "command": "ACTIVATE"}, {"target": "STRAT_REVERSION", "command": "ORPHAN"}]
            elif regime == "CHOP":
                commands = [{"target": "STRAT_GAMMA", "command": "ORPHAN"}, {"target": "STRAT_REVERSION", "command": "ACTIVATE"}]
            else:
                commands = [{"target": "STRAT_GAMMA", "command": "ORPHAN"}, {"target": "STRAT_REVERSION", "command": "ORPHAN"}]
        
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
                    kill_reason = self.check_kill_switches(state)
                    regime = self.detect_regime(state)
                    
                    await self.broadcast(regime, kill_reason)
                    
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error in MetaRouter: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    router = MetaRouter()
    asyncio.run(router.run())
