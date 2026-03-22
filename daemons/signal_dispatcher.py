"""
Signal Dispatcher — Reads SHM once per tick, broadcasts frozen SignalSnapshot.
Eliminates per-Knight SHM reads and prevents tick-dict mutation side effects.
"""

import math
import time
import logging

from core.shm import ShmManager, RegimeShm, is_shm_missing
from core.strategy.base_knight import SignalSnapshot

logger = logging.getLogger("SignalDispatcher")


class SignalDispatcher:
    """Single reader for SHM + Regime. Broadcasts frozen SignalSnapshot."""

    def __init__(
        self,
        shm_alpha_managers: dict[str, ShmManager],
        shm_regime_managers: dict[str, RegimeShm],
        redis_client=None,
    ):
        self._alpha = shm_alpha_managers
        self._regime = shm_regime_managers
        self._redis = redis_client

        # Cached values (refreshed periodically, not per-tick)
        self._halt_kinetic = False
        self._macro_lockdown = False
        self._iv_cache_pct = 15.0
        self._vwap_cache: dict[str, float] = {}
        self._last_cache_refresh = 0.0

    async def _refresh_caches(self, symbol: str):
        """Refresh slow-moving Redis caches every 5 seconds."""
        now = time.time()
        if now - self._last_cache_refresh < 5:
            return

        if self._redis:
            halt_val = await self._redis.get("HALT_KINETIC")
            self._halt_kinetic = halt_val == "True"

            lockdown_val = await self._redis.get("MACRO_EVENT_LOCKDOWN")
            self._macro_lockdown = lockdown_val == "True"

            iv_raw = await self._redis.get(f"atm_iv:{symbol}") or await self._redis.get("atm_iv")
            self._iv_cache_pct = float(iv_raw or 0.15) * 100.0

            vwap_raw = await self._redis.get(f"VWAP:{symbol}")
            self._vwap_cache[symbol] = float(vwap_raw or 0.0)

        self._last_cache_refresh = now

    async def build_snapshot(
        self,
        symbol: str,
        tick: dict,
    ) -> SignalSnapshot:
        """Build a frozen SignalSnapshot from SHM + tick + Redis caches.
        
        Called once per tick. All Knights receive the exact same snapshot.
        """
        await self._refresh_caches(symbol)

        # ── SHM Alpha ────────────────────────────────────────────────────
        sig = {}
        if symbol in self._alpha:
            shm = self._alpha[symbol]
            sig = shm.read_dict() if hasattr(shm, 'read_dict') else {}
        elif "GLOBAL" in self._alpha:
            shm = self._alpha["GLOBAL"]
            sig = shm.read_dict() if hasattr(shm, 'read_dict') else {}

        # ── SHM Regime ───────────────────────────────────────────────────
        regime = None
        if symbol in self._regime:
            regime = self._regime[symbol].read()
        # Fallback to index-level regime
        elif "BANKNIFTY" in symbol and "BANKNIFTY" in self._regime:
            regime = self._regime["BANKNIFTY"].read()
        elif "NIFTY" in symbol and "NIFTY50" in self._regime:
            regime = self._regime["NIFTY50"].read()

        # ── Extract fields with sentinel awareness ───────────────────────
        def safe_float(key: str, default: float = 0.0) -> float:
            val = sig.get(key, default)
            return default if is_shm_missing(val) else float(val)

        s_total = safe_float("s_total")
        asto = safe_float("asto")
        smart_flow = safe_float("smart_flow")
        whale_pivot = safe_float("whale_pivot")
        vpin = safe_float("vpin")
        adx = safe_float("adx")
        atr = safe_float("atr", 20.0)
        iv_rv = safe_float("iv_rv_spread")

        # ── Regime fields ────────────────────────────────────────────────
        s18 = regime.get("regime_s18", 0) if isinstance(regime, dict) else (regime.regime_s18 if regime else 0)
        s27 = regime.get("quality_s27", 100.0) if isinstance(regime, dict) else (regime.quality_s27 if regime else 100.0)
        s26 = regime.get("persistence_s26", 0.0) if isinstance(regime, dict) else (regime.persistence_s26 if regime else 0.0)
        is_toxic = bool(sig.get("veto", False))

        # ── Latency Decay (applied once, not per-Knight) ─────────────────
        latency_ms = float(tick.get("latency_ms", 0.0))
        if latency_ms > 50:
            decay_factor = math.exp(-0.005 * (latency_ms - 50))
            s_total *= decay_factor
            asto *= decay_factor

        # ── Z-Score from high_z / low_z ──────────────────────────────────
        high_z = safe_float("high_z")
        low_z = safe_float("low_z")
        price_zscore = high_z if abs(high_z) > abs(low_z) else low_z

        # ── HW Alphas ────────────────────────────────────────────────────
        hw_raw = sig.get("hw_alpha", [])
        hw_alphas = tuple(hw_raw) if isinstance(hw_raw, list) else ()

        price = float(tick.get("price", 0.0))
        vwap = self._vwap_cache.get(symbol, price)

        # ── Inception spot for 0DTE ──────────────────────────────────────
        inception_spot = 0.0
        if self._redis:
            try:
                spot_raw = await self._redis.get(f"ltp:{symbol}")
                inception_spot = float(spot_raw or 0.0)
            except Exception:
                pass

        return SignalSnapshot(
            symbol=symbol,
            price=price,
            timestamp=tick.get("timestamp", ""),
            s18=s18,
            s27=s27,
            s26=s26,
            asto=asto,
            alpha_total=s_total,
            smart_flow=smart_flow,
            whale_pivot=whale_pivot,
            vpin=vpin,
            adx=adx,
            atr=atr,
            iv_rv_spread=iv_rv,
            iv_atm=self._iv_cache_pct / 100.0,
            price_zscore=price_zscore,
            vwap=vwap,
            hw_alphas=hw_alphas,
            latency_ms=latency_ms,
            is_toxic=is_toxic,
            sequence_id=tick.get("sequence_id", 0),
            halt_kinetic=self._halt_kinetic,
            macro_lockdown=self._macro_lockdown,
            iv_cache_pct=self._iv_cache_pct,
            inception_spot=inception_spot,
        )
