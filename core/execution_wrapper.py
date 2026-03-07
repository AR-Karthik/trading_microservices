import asyncio
import logging
import uuid
from typing import List, Dict

logger = logging.getLogger(__name__)

class MultiLegExecutor:
    """
    Handles 'Atomic' execution of multiple orders simultaneously.
    Useful for straddles, spreads, or hedging where legs must be placed as close
    together in time as possible.
    """
    def __init__(self, execution_engine):
        self.engine = execution_engine

    async def execute_legs(self, orders: List[Dict]):
        """
        Executes multiple orders in parallel using asyncio.gather.
        """
        if not orders:
            return []

        logger.info(f"⚡ Executing {len(orders)} legs atomically...")
        
        # Fire all legs simultaneously
        tasks = [self.engine.place_order(order) for order in orders]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Log results
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"Leg {i} failed: {res}")
            else:
                logger.info(f"Leg {i} executed: {res}")
                
        return results
