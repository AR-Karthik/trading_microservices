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

# Reserve: subtracts `amount` from `AVAILABLE_MARGIN_[TYPE]` only if the resulting
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

# Release: adds `amount` back to `AVAILABLE_MARGIN_[TYPE]`.
# Used when closing a position (margin + pnl) or refunding a phantom order.
LUA_RELEASE = """
local available = tonumber(redis.call('get', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local global_limit = tonumber(redis.call('get', KEYS[2]) or '0')

local new_available = available + amount
-- Optional: Cap available margin at global_limit or let profits grow it?
-- Decided to let profits grow the available margin pool. 
redis.call('set', KEYS[1], new_available)
return tostring(new_available)
"""

class MarginManager:
    """Manages the atomic Redis locks for capital allocation."""
    
    def __init__(self, redis_client: Redis):
        self.r = redis_client
        self._reserve_script = self.r.register_script(LUA_RESERVE)
        self._release_script = self.r.register_script(LUA_RELEASE)

    def _get_keys(self, execution_type: str) -> list[str]:
        suffix = "LIVE" if execution_type.upper() == "ACTUAL" else "PAPER"
        return [f"AVAILABLE_MARGIN_{suffix}", f"GLOBAL_CAPITAL_LIMIT_{suffix}"]

    def reserve(self, required_margin: float, execution_type: str = "Paper") -> bool:
        """
        Atomically attempts to reserve margin for the specific execution type. 
        Returns True if successful, False if breached.
        """
        try:
            keys = self._get_keys(execution_type)
            result = self._reserve_script(
                keys=[keys[0]],
                args=[required_margin]
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Error executing LUA_RESERVE script: {e}")
            return False

    def release(self, amount: float, execution_type: str = "Paper") -> float:
        """
        Atomically releases margin back into the pool for the specific execution type.
        """
        try:
            keys = self._get_keys(execution_type)
            new_available = self._release_script(
                keys=keys,
                args=[amount]
            )
            return float(new_available)
        except Exception as e:
            logger.error(f"Error executing LUA_RELEASE script: {e}")
            return 0.0

    def get_available(self, execution_type: str = "Paper") -> float:
        """Fetches the current available margin in real-time."""
        try:
            keys = self._get_keys(execution_type)
            return float(self.r.get(keys[0]) or 0.0)
        except Exception:
            return 0.0


class AsyncMarginManager:
    """Async counterpart for daemons using redis.asyncio (like order_reconciler)."""
    
    def __init__(self, async_redis_client):
        self.r = async_redis_client
        self._reserve_script = self.r.register_script(LUA_RESERVE)
        self._release_script = self.r.register_script(LUA_RELEASE)

    def _get_keys(self, execution_type: str) -> list[str]:
        suffix = "LIVE" if execution_type.upper() == "ACTUAL" else "PAPER"
        return [f"AVAILABLE_MARGIN_{suffix}", f"GLOBAL_CAPITAL_LIMIT_{suffix}"]

    async def reserve(self, required_margin: float, execution_type: str = "Paper") -> bool:
        try:
            keys = self._get_keys(execution_type)
            result = await self._reserve_script(
                keys=[keys[0]],
                args=[required_margin]
            )
            return bool(result)
        except Exception as e:
            logger.error(f"Error executing LUA_RESERVE script: {e}")
            return False

    async def release(self, amount: float, execution_type: str = "Paper") -> float:
        try:
            keys = self._get_keys(execution_type)
            new_available = await self._release_script(
                keys=keys,
                args=[amount]
            )
            return float(new_available)
        except Exception as e:
            logger.error(f"Error executing LUA_RELEASE script: {e}")
            return 0.0

    async def get_available(self, execution_type: str = "Paper") -> float:
        try:
            keys = self._get_keys(execution_type)
            val = await self.r.get(keys[0])
            return float(val or 0.0)
        except Exception:
            return 0.0
