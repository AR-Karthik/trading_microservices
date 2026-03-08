"""
core/margin.py
===============
Atomic margin reservation and release mechanisms using Redis Lua scripts.
Ensures hard global budget constraints across multiprocessing barriers.
"""

import logging
from redis import Redis

logger = logging.getLogger("MarginManager")

# ── Lua Scripts ──────────────────────────────────────────────────────────────

# Reserve: subtracts `amount` from `AVAILABLE_MARGIN` only if the resulting
# margin would be >= 0. Returns 1 if successful, 0 if insufficient margin.
LUA_RESERVE = """
local available = tonumber(redis.call('get', KEYS[1]) or '0')
local required = tonumber(ARGV[1])

if available >= required then
    redis.call('set', KEYS[1], available - required)
    return 1
else
    return 0
end
"""

# Release: adds `amount` back to `AVAILABLE_MARGIN`.
# Used when closing a position (margin + pnl) or refunding a phantom order.
LUA_RELEASE = """
local available = tonumber(redis.call('get', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local global_limit = tonumber(redis.call('get', KEYS[2]) or '0')

local new_available = available + amount
-- Optional: Cap available margin at global_limit, or let profits grow it?
-- Decided to let profits grow the available margin pool. 
-- However, you could cap it here if explicitly required.
redis.call('set', KEYS[1], new_available)
return tostring(new_available)
"""

class MarginManager:
    """Manages the atomic Redis locks for capital allocation."""
    
    def __init__(self, redis_client: Redis):
        self.r = redis_client
        self._reserve_script = self.r.register_script(LUA_RESERVE)
        self._release_script = self.r.register_script(LUA_RELEASE)

    def reserve(self, required_margin: float) -> bool:
        """
        Atomically attempts to reserve margin. 
        Returns True if successful, False if breached.
        """
        try:
            result = self._reserve_script(
                keys=["AVAILABLE_MARGIN"],
                args=[required_margin]
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Error executing LUA_RESERVE script: {e}")
            return False

    def release(self, amount: float) -> float:
        """
        Atomically releases margin back into the pool.
        Amount should be the original margin reserved, plus any realized P&L.
        """
        try:
            new_available = self._release_script(
                keys=["AVAILABLE_MARGIN", "GLOBAL_CAPITAL_LIMIT"],
                args=[amount]
            )
            return float(new_available)
        except Exception as e:
            logger.error(f"Error executing LUA_RELEASE script: {e}")
            return 0.0

    def get_available(self) -> float:
        """Fetches the current available margin in real-time."""
        try:
            return float(self.r.get("AVAILABLE_MARGIN") or 0.0)
        except Exception:
            return 0.0


class AsyncMarginManager:
    """Async counterpart for daemons using redis.asyncio (like order_reconciler)."""
    
    def __init__(self, async_redis_client):
        self.r = async_redis_client
        self._reserve_script = self.r.register_script(LUA_RESERVE)
        self._release_script = self.r.register_script(LUA_RELEASE)

    async def reserve(self, required_margin: float) -> bool:
        try:
            result = await self._reserve_script(
                keys=["AVAILABLE_MARGIN"],
                args=[required_margin]
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Error executing LUA_RESERVE script: {e}")
            return False

    async def release(self, amount: float) -> float:
        try:
            new_available = await self._release_script(
                keys=["AVAILABLE_MARGIN", "GLOBAL_CAPITAL_LIMIT"],
                args=[amount]
            )
            return float(new_available)
        except Exception as e:
            logger.error(f"Error executing LUA_RELEASE script: {e}")
            return 0.0

    async def get_available(self) -> float:
        try:
            val = await self.r.get("AVAILABLE_MARGIN")
            return float(val or 0.0)
        except Exception:
            return 0.0
