"""
Portfolio State Manager — Module 6 Decomposition
Owns the in-memory position map, DB hydration, and Redis sync.
The LiquidationDaemon becomes stateless — it queries this layer for targets.
"""

import asyncio
import time
import logging

import asyncpg
import redis.asyncio as redis

from core.db_retry import with_db_retry
from core.math.risk_utils import determine_underlying, pre_calculate_thresholds

logger = logging.getLogger("PortfolioState")


class PortfolioState:
    """Manages the in-memory portfolio of active positions.
    
    Responsibilities:
      - Hydration from TimescaleDB on startup
      - Specific position hydration on NEW_POSITION_ALERT
      - Periodic DB reconciliation (60s)
      - Pre-calculated threshold computation on arm
      - Position CRUD (get, update, remove, iterate)
    """

    def __init__(self, redis_client: redis.Redis, pool: asyncpg.Pool):
        self._redis = redis_client
        self.pool = pool
        # {symbol: {qty, entry_price, action, entry_time, lifecycle_class, ...thresholds}}
        self.positions: dict[str, dict] = {}

    # ── Pool Lifecycle ────────────────────────────────────────────────────────

    async def reconnect_pool(self):
        """Reconnect the asyncpg pool."""
        try:
            if self.pool:
                await self.pool.close()
            from core.auth import get_db_dsn
            dsn = get_db_dsn()
            self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
            logger.info("✅ Portfolio DB Pool reconnected.")
        except Exception as e:
            logger.error(f"❌ Failed to reconnect Portfolio DB pool: {e}")
            raise

    # ── Full Hydration ────────────────────────────────────────────────────────

    async def hydrate_from_db(self):
        """Reconstruct the full portfolio state from TimescaleDB."""
        logger.info("Hydrating portfolio state from TimescaleDB...")
        try:
            async with self.pool.acquire() as conn:
                await self._do_hydrate(conn)
            logger.info(f"✅ Hydrated {len(self.positions)} positions from DB.")
        except Exception as e:
            logger.error(f"Hydration failed: {e}")

    @with_db_retry(max_retries=3, backoff=0.5)
    async def _do_hydrate(self, conn):
        rows = await conn.fetch("SELECT * FROM portfolio WHERE quantity != 0")
        for row in rows:
            sym = row['symbol']
            pos = self._row_to_position(sym, row)
            self.positions[sym] = pos
            await self._compute_thresholds(pos)

    # ── Specific Position Hydration ───────────────────────────────────────────

    async def hydrate_specific(self, symbol: str):
        """Hydrate a single position from DB (used on NEW_POSITION_ALERT)."""
        try:
            async with self.pool.acquire() as conn:
                await self._do_hydrate_specific(conn, symbol)
        except Exception as e:
            logger.error(f"Specific hydration failed for {symbol}: {e}")

    @with_db_retry(max_retries=3, backoff=0.5)
    async def _do_hydrate_specific(self, conn, symbol: str):
        row = await conn.fetchrow(
            "SELECT * FROM portfolio WHERE symbol=$1 AND quantity != 0", symbol
        )
        if row:
            pos = self._row_to_position(symbol, row)
            self.positions[symbol] = pos
            await self._compute_thresholds(pos)
            logger.info(f"🎯 POSITION ARMED: {symbol} [{row['lifecycle_class']}]")

    # ── Event-Driven Arming (No DB round-trip) ────────────────────────────────

    async def arm_from_event(self, data: dict):
        """Arm a position directly from a NEW_POSITION_ALERT payload.
        
        This bypasses the DB round-trip for instant arming. A background
        DB validation is scheduled to reconcile.
        """
        symbol = data.get("symbol")
        if not symbol:
            return

        inception_spot = float(data.get("inception_spot", 0.0))
        if inception_spot <= 0:
            underlying = determine_underlying(symbol)
            spot_raw = await self._redis.get(f"ltp:{underlying}")
            inception_spot = float(spot_raw) if spot_raw else 0.0

        pos = {
            "symbol": symbol,
            "quantity": float(data.get("quantity", 0)),
            "price": float(data.get("avg_price", data.get("price", 0))),
            "entry_time": data.get("entry_time", time.time()),
            "lifecycle_class": data.get("lifecycle_class", "KINETIC"),
            "parent_uuid": data.get("parent_uuid"),
            "expiry_date": data.get("expiry_date"),
            "initial_credit": float(data.get("initial_credit", 0.0)),
            "short_strikes": data.get("short_strikes", {}),
            "execution_type": data.get("execution_type", "Paper"),
            "action": data.get("action", "BUY"),
            "inception_spot": inception_spot,
        }
        self.positions[symbol] = pos
        await self._compute_thresholds(pos)
        logger.info(f"⚡ POSITION ARMED (Event): {symbol} [{pos['lifecycle_class']}]")
        
        # Background DB validation (non-blocking)
        asyncio.create_task(self._validate_against_db(symbol))

    async def _validate_against_db(self, symbol: str):
        """Background task to validate event-armed position against DB."""
        await asyncio.sleep(2.0)  # Give Bridge time to write
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM portfolio WHERE symbol=$1 AND quantity != 0", symbol
                )
                if row:
                    db_qty = float(row['quantity'])
                    mem_qty = self.positions.get(symbol, {}).get("quantity", 0)
                    if db_qty != mem_qty:
                        logger.warning(
                            f"⚠️ DB Reconciliation mismatch for {symbol}: "
                            f"mem={mem_qty}, db={db_qty}. Using DB value."
                        )
                        pos = self._row_to_position(symbol, row)
                        self.positions[symbol] = pos
                        await self._compute_thresholds(pos)
                else:
                    logger.debug(f"DB validation: {symbol} not found in DB yet (may be in-flight).")
        except Exception as e:
            logger.error(f"Background DB validation failed for {symbol}: {e}")

    # ── Periodic Sync ─────────────────────────────────────────────────────────

    async def periodic_sync(self, interval: int = 60):
        """Continuous portfolio reconciliation loop."""
        while True:
            try:
                await asyncio.sleep(interval)
                logger.info("🔄 Periodic Portfolio Hydration triggered...")
                await self.hydrate_from_db()
            except Exception as e:
                logger.error(f"Error in periodic hydration: {e}")

    # ── Position Access ───────────────────────────────────────────────────────

    def get_position(self, symbol: str) -> dict | None:
        return self.positions.get(symbol)

    def get_all_positions(self) -> dict[str, dict]:
        return self.positions

    def get_positions_by_underlying(self, underlying: str) -> list[tuple[str, dict]]:
        """Get all positions whose symbol maps to the given underlying."""
        return [
            (sym, pos) for sym, pos in self.positions.items()
            if determine_underlying(sym) == underlying
        ]

    def get_basket(self, parent_uuid: str) -> list[dict]:
        """Get all legs in a position basket."""
        return [p for p in self.positions.values() if p.get("parent_uuid") == parent_uuid]

    def remove_position(self, symbol: str):
        self.positions.pop(symbol, None)

    def update_position(self, symbol: str, updates: dict):
        pos = self.positions.get(symbol)
        if pos:
            pos.update(updates)

    def has_positions(self) -> bool:
        return bool(self.positions)

    # ── Internal Helpers ──────────────────────────────────────────────────────

    def _row_to_position(self, symbol: str, row) -> dict:
        """Convert a DB row to a position dict."""
        return {
            "symbol": symbol,
            "quantity": float(row['quantity']),
            "price": float(row['avg_price']),
            "entry_time": row['entry_time'].timestamp() if row['entry_time'] else time.time(),
            "lifecycle_class": row['lifecycle_class'],
            "parent_uuid": row['parent_uuid'],
            "expiry_date": row['expiry_date'],
            "initial_credit": float(row['initial_credit'] or 0.0),
            "short_strikes": row['short_strikes'] or {},
            "execution_type": row['execution_type'] or "Paper",
            "inception_spot": float(row['inception_spot'] or 0.0),
        }

    async def _compute_thresholds(self, pos: dict):
        """Pre-calculate hard price triggers for fast tick-loop comparison."""
        entry = float(pos.get("price", 0))
        action = pos.get("action", "BUY")
        underlying = determine_underlying(pos.get("symbol", ""))
        
        # Fetch ATR and VIX from Redis
        atr_raw = await self._redis.get(f"ATR:{underlying}")
        atr = float(atr_raw) if atr_raw else 20.0
        
        vix_raw = await self._redis.get(f"vix:{underlying}") or await self._redis.get("vix")
        vix = float(vix_raw) if vix_raw else 15.0
        
        rv_raw = await self._redis.get(f"rv:{underlying}") or await self._redis.get("rv")
        rv = float(rv_raw) if rv_raw else 0.0
        
        thresholds = pre_calculate_thresholds(entry, atr, action, rv, vix)
        pos.update(thresholds)
        pos["_atr"] = atr
        pos["_vix"] = vix
        pos["_rv"] = rv
