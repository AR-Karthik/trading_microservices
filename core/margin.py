"""
core/margin.py [PHASE 23.0] SEALED INSTITUTIONAL VERSION
Atomic margin reservation and release mechanisms with Self-Healing LUA.
"""

import logging
import asyncio
from typing import Dict, Tuple

logger = logging.getLogger("MarginManager")

# ── Hardened Lua Scripts ──────────────────────────────────────────────────────

LUA_GET_STATE = """
local cash = tonumber(redis.call('get', KEYS[1]) or '0')
local coll = tonumber(redis.call('get', KEYS[2]) or '0')
return {tostring(cash), tostring(coll)}
"""

LUA_SYNC_CAPITAL = """
-- KEYS[1]: CASH, KEYS[2]: COLLATERAL, KEYS[3]: LIMIT
-- ARGV[1]: Delta Cash, ARGV[2]: Delta Collateral, ARGV[3]: New Limit
local cash = tonumber(redis.call('get', KEYS[1]) or '0')
local coll = tonumber(redis.call('get', KEYS[2]) or '0')
redis.call('set', KEYS[1], tostring(cash + tonumber(ARGV[1])))
redis.call('set', KEYS[2], tostring(coll + tonumber(ARGV[2])))
redis.call('set', KEYS[3], tostring(ARGV[3]))
return 1
"""

LUA_RESERVE = """
-- KEYS[1]: CASH, KEYS[2]: COLLATERAL | ARGV[1]: Required
local cash_avail = tonumber(redis.call('get', KEYS[1]) or '0')
local coll_avail = tonumber(redis.call('get', KEYS[2]) or '0')
local req = tonumber(ARGV[1])

if (cash_avail + coll_avail) < req then
    return -1 -- Insufficient Total Margin
end

if cash_avail < (req * 0.5) then
    return -2 -- Insufficient Liquid Cash (50:50 breach)
end

-- Functionality: Drain collateral pool first (up to 50% of req) to save cash
local coll_use = math.min(coll_avail, req * 0.5)
local cash_use = req - coll_use

redis.call('set', KEYS[1], tostring(cash_avail - cash_use))
redis.call('set', KEYS[2], tostring(coll_avail - coll_use))
return 1
"""

LUA_RELEASE = """
-- KEYS[1]: CASH, KEYS[2]: COLLATERAL, KEYS[3]: TOTAL_LIMIT
-- ARGV[1]: Amount back, ARGV[2]: Original Margin reserved
local cash_avail = tonumber(redis.call('get', KEYS[1]) or '0')
local coll_avail = tonumber(redis.call('get', KEYS[2]) or '0')
local limit = tonumber(redis.call('get', KEYS[3]) or '0')
local amount_back = tonumber(ARGV[1])
local original = tonumber(ARGV[2])

-- PhD Institutional Rule: Profits (any amount above original) MUST go to Cash.
-- Healing Rule: Restore cash to 50% of original first.
local profit = math.max(0, amount_back - original)
local principal_back = amount_back - profit

local cash_ret = (principal_back * 0.5) + profit
local coll_ret = (principal_back * 0.5)

local new_cash = cash_avail + cash_ret
local new_coll = coll_avail + coll_ret

-- Ceiling Guard: Prevent double-release or record corruption
-- PhD Rule: 1.02 tolerance (2%) to keep accounting logic mathematically rigid.
if limit > 0 and (new_cash + new_coll) > (limit * 1.02) then
    local excess = (new_cash + new_coll) - limit
    new_cash = math.max(0, new_cash - excess) 
end

redis.call('set', KEYS[1], tostring(new_cash))
redis.call('set', KEYS[2], tostring(new_coll))
return tostring(new_cash + new_coll)
"""

class MarginManager:
    def __init__(self, async_redis_client):
        self.r = async_redis_client
        self._reserve_script = self.r.register_script(LUA_RESERVE)
        self._release_script = self.r.register_script(LUA_RELEASE)
        self._state_script = self.r.register_script(LUA_GET_STATE)
        self._sync_script = self.r.register_script(LUA_SYNC_CAPITAL)

    def _get_suffix(self, ex_type: str) -> str:
        """Standardize 'Actual' and 'Live' to the 'LIVE' suffix used in Redis keys."""
        return "LIVE" if ex_type.upper() in ["ACTUAL", "LIVE"] else "PAPER"

    def _get_keys(self, execution_type: str) -> list[str]:
        suffix = self._get_suffix(execution_type)
        return [f"CASH_COMPONENT_{suffix}", f"COLLATERAL_COMPONENT_{suffix}"]

    def _get_limit_key(self, execution_type: str) -> str:
        return f"{self._get_suffix(execution_type)}_CAPITAL_LIMIT"

    async def reserve(self, required_margin: float, execution_type: str = "Paper") -> int:
        """ Returns: 1 (Success), -1 (Total Insufficient), -2 (Cash Ratio Breach). """
        try:
            keys = self._get_keys(execution_type)
            return int(await self._reserve_script(keys=keys, args=[required_margin]))
        except Exception as e:
            logger.error(f"LUA_RESERVE error: {e}")
            return -1

    async def release(self, amount: float, original_margin: float = 0.0, execution_type: str = "Paper") -> float:
        try:
            keys = self._get_keys(execution_type)
            keys.append(self._get_limit_key(execution_type))
            
            # Fallback for legacy bridge pulses
            orig = original_margin if original_margin > 0 else amount
            
            # Phase 23.1: Clipping vs Breaching Audit
            # The LUA now clips excess internally to prevent overflow.
            # Python just accepts the final resulting float sum.
            res = await self._release_script(keys=keys, args=[amount, orig])
            return float(res)
        except Exception as e:
            logger.error(f"LUA_RELEASE error: {e}")
            return 0.0

    async def get_state(self, execution_type: str = "Paper") -> Dict[str, float]:
        """Atomic read of current vault balances."""
        try:
            keys = self._get_keys(execution_type)
            cash, coll = await self._state_script(keys=keys)
            return {"cash": float(cash), "collateral": float(coll), "total": float(cash) + float(coll)}
        except Exception:
            return {"cash": 0.0, "collateral": 0.0, "total": 0.0}

    async def sync_capital(self, delta_cash: float, delta_coll: float, new_limit: float, execution_type: str = "Paper"):
        """Atomic delta adjustment to prevent race conditions with open reservations."""
        try:
            keys = self._get_keys(execution_type)
            keys.append(self._get_limit_key(execution_type))
            await self._sync_script(keys=keys, args=[delta_cash, delta_coll, new_limit])
            logger.info(f"Vault Adjusted [{execution_type}]: CashΔ={delta_cash}, CollΔ={delta_coll}, Limit={new_limit}")
        except Exception as e:
            logger.error(f"LUA_SYNC_CAPITAL error: {e}")

# Standardize Class Name across framework
AsyncMarginManager = MarginManager
