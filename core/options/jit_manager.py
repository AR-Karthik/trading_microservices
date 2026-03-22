"""
JIT Warmup Manager — Pre-subscribe to nearby strikes before entry triggers.
When ASTO crosses the "pre-signal" threshold (|ASTO| > 60), proactively
subscribe to the nearest N strikes so the first trade has zero first-tick latency.
"""

import asyncio
import time
import logging
from core.options.chain_utils import get_increment, snap_strike, get_index_meta

logger = logging.getLogger("JITWarmup")


class JITWarmupManager:
    """Proactive strike subscription manager.
    
    Monitors ASTO and pre-subscribes to nearby strikes when ASTO crosses
    the pre-signal threshold (~60), BEFORE the actual entry trigger (~75).
    This reduces "first-trade latency" to near-zero.
    """

    def __init__(self, redis_client, gateway_publisher=None, n_strikes: int = 5):
        self._redis = redis_client
        self._gateway = gateway_publisher  # ZMQ socket to Shoonya Gateway
        self._n_strikes = n_strikes
        self._warmed: dict[str, set[float]] = {}  # symbol → set of warmed strikes
        self._pre_signal_threshold = 60  # |ASTO| threshold for pre-subscription
        self._cooldown_sec = 30
        self._last_warmup: dict[str, float] = {}

    async def check_and_warmup(self, symbol: str, asto: float, price: float):
        """Check if ASTO crossed pre-signal threshold and warm up strikes.
        
        Called by the SignalDispatcher or Registry on each tick.
        """
        if abs(asto) < self._pre_signal_threshold:
            return

        # Cooldown: don't re-warm the same symbol too frequently
        now = time.time()
        if now - self._last_warmup.get(symbol, 0) < self._cooldown_sec:
            return

        meta = get_index_meta(symbol)
        increment = meta["increment"]
        atm = snap_strike(price, increment)

        # Generate N strikes above and below ATM
        strikes_to_warm = set()
        for i in range(-self._n_strikes, self._n_strikes + 1):
            strike = atm + (i * increment)
            for otype in ("CE", "PE"):
                strikes_to_warm.add((strike, otype))

        # Check which strikes are already warmed
        already_warmed = self._warmed.get(symbol, set())
        new_strikes = {s for s in strikes_to_warm if s[0] not in already_warmed}

        if not new_strikes:
            return

        # Subscribe via Redis command queue (Gateway picks these up)
        for strike, otype in new_strikes:
            sub_key = f"WARMUP_SUB:{symbol}:{int(strike)}{otype}"
            try:
                await self._redis.set(sub_key, "1", ex=300)  # 5-min TTL
            except Exception as e:
                logger.error(f"JIT warmup subscribe error: {e}")
                continue

        # Track warmed strikes
        if symbol not in self._warmed:
            self._warmed[symbol] = set()
        self._warmed[symbol].update(s[0] for s in new_strikes)
        self._last_warmup[symbol] = now

        logger.info(
            f"🔥 JIT WARMUP: Pre-subscribed {len(new_strikes)} strikes for {symbol} "
            f"(ASTO={asto:.1f}, ATM={atm})"
        )

    def clear_warmup(self, symbol: str):
        """Clear warmed strikes for a symbol (e.g., after position close)."""
        self._warmed.pop(symbol, None)
        self._last_warmup.pop(symbol, None)
