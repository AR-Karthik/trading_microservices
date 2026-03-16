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

# Reserve: subtracts `amount` from appropriate pools.
# Enforces the 50:50 Cash-to-Collateral rule: At least 50% of the required margin 
# must be available in the CASH pool.
LUA_RESERVE = """
local cash_avail = tonumber(redis.call('get', KEYS[1]) or '0')
local coll_avail = tonumber(redis.call('get', KEYS[2]) or '0')
local required = tonumber(ARGV[1])

local cash_needed = required * 0.5
local total_needed = required

if cash_avail >= cash_needed and (cash_avail + coll_avail) >= total_needed then
    -- Use up to 50% from collateral first, then the rest from cash
    local coll_use = math.min(coll_avail, required * 0.5)
    local cash_use = required - coll_use
    
    redis.call('set', KEYS[1], cash_avail - cash_use)
    redis.call('set', KEYS[2], coll_avail - coll_use)
    return 1
else
    return 0
end
"""

# Release: adds `amount` back to the pools.
# Profit is proportionally returned to the CASH pool to maintain regulatory liquidity.
LUA_RELEASE = """
local cash_avail = tonumber(redis.call('get', KEYS[1]) or '0')
local coll_avail = tonumber(redis.call('get', KEYS[2]) or '0')
local amount = tonumber(ARGV[1])

-- Logic: Return exactly half to cash, half to collateral if possible, 
-- or prioritize restoring cash to maintain the 50:50 buffer.
local cash_back = amount * 0.5
local coll_back = amount * 0.5

redis.call('set', KEYS[1], cash_avail + cash_back)
redis.call('set', KEYS[2], coll_avail + coll_back)
return tostring(cash_avail + coll_avail + amount)
"""

class MarginManager:
    """Async manager for the atomic Redis locks for capital allocation [Audit 14.4]."""
    
    def __init__(self, async_redis_client):
        self.r = async_redis_client
        self._reserve_script = self.r.register_script(LUA_RESERVE)
        self._release_script = self.r.register_script(LUA_RELEASE)

    def _get_keys(self, execution_type: str) -> list[str]:
        suffix = "LIVE" if execution_type.upper() == "ACTUAL" else "PAPER"
        return [f"CASH_COMPONENT_{suffix}", f"COLLATERAL_COMPONENT_{suffix}"]

    async def reserve(self, required_margin: float, execution_type: str = "Paper") -> bool:
        try:
            keys = self._get_keys(execution_type)
            result = await self._reserve_script(
                keys=keys,
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
            cash = float(await self.r.get(keys[0]) or 0.0)
            coll = float(await self.r.get(keys[1]) or 0.0)
            return cash + coll
        except Exception:
            return 0.0

# Legacy compatibility alias [Audit 14.4]
AsyncMarginManager = MarginManager
