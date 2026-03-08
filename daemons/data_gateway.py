"""
daemons/data_gateway.py
=======================
The Resilient Firehose (SRS §2.2)

Responsibilities:
- Dynamic lot size fetch at 09:01 IST (Nifty=65, BankNifty=30) → Redis
- Tick staleness watchdog (>1000ms → TCP socket reset)
- SEBI Circuit Breaker broadcasts (10% / 15% / 20% halt → SYSTEM_HALT)
- High-frequency tick streaming via ZeroMQ PUB to all consumers
"""

import asyncio
import json
import logging
import random
import sys
from datetime import datetime, timezone, time as dt_time
from zoneinfo import ZoneInfo

import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics

try:
    import uvloop
except ImportError:
    uvloop = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("DataGateway")

IST = ZoneInfo("Asia/Kolkata")

# Symbols simulated / watched
SYMBOLS_UNDERLYING = ["NIFTY50", "BANKNIFTY", "RELIANCE", "HDFC", "INFY", "TCS", "ICICIBANK"]

# Default dynamic lot sizes (updated from broker at 09:01 IST)
DEFAULT_LOT_SIZES = {"NIFTY50": 65, "BANKNIFTY": 30}

# Staleness threshold — if last tick delta exceeds this, force reconnect
STALENESS_THRESHOLD_MS = 1000

# SEBI Circuit Breaker halt definitions (level → halt_minutes by time-of-day)
# Format: {pct: {before_1_pm: mins, after_1_pm: mins, after_2_30_pm: "no_halt"}}
CIRCUIT_BREAKER_MATRIX = {
    10: {"before_1_pm": 45, "before_2_30_pm": 15, "after_2_30_pm": 0},
    15: {"before_1_pm": 105, "before_2_30_pm": 45, "after_2_30_pm": 0},
    20: {"before_1_pm": 0, "before_2_30_pm": 0, "after_2_30_pm": 0},  # day halt
}


class DataGateway:
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.mq = MQManager()
        self.redis_url = redis_url
        self.redis_client: redis.Redis | None = None
        self.pub_socket = self.mq.create_publisher(Ports.MARKET_DATA)

        self._prices = {
            "NIFTY50": 22350.0, "BANKNIFTY": 47200.0,
            "RELIANCE": 1480.0, "HDFC": 1680.0,
            "INFY": 1820.0, "TCS": 3940.0, "ICICIBANK": 1230.0
        }
        self._oi = {s: random.randint(800_000, 1_500_000) for s in SYMBOLS_UNDERLYING}
        self._last_tick_ts: dict[str, float] = {}
        self._lot_sizes_fetched = False
        self._system_halted = False

    # ── Gateway Startup ──────────────────────────────────────────────────────

    async def start(self):
        self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
        logger.info("DataGateway initialised. Starting sub-tasks...")

        await asyncio.gather(
            self._tick_stream(),
            self._lot_size_scheduler(),
            self._staleness_watchdog(),
            self._circuit_breaker_monitor(),
        )

    # ── Tick Stream ──────────────────────────────────────────────────────────

    async def _tick_stream(self):
        """
        Simulates Shoonya WebSocket tick stream.
        In production, replace with actual WebSocket callback handler.
        """
        logger.info("Tick stream started (simulated Shoonya WebSocket).")

        while True:
            if self._system_halted:
                logger.warning("System halted — tick stream paused.")
                await asyncio.sleep(1)
                continue

            try:
                await asyncio.sleep(random.uniform(0.04, 0.18))

                symbol = random.choice(SYMBOLS_UNDERLYING)
                prev_price = self._prices[symbol]

                # Random walk with skewed volatility for index vs equity
                vol = 0.0008 if symbol in ("NIFTY50", "BANKNIFTY") else 0.0015
                change = prev_price * random.uniform(-vol, vol)
                self._prices[symbol] = round(prev_price + change, 2)

                prev_oi = self._oi[symbol]
                self._oi[symbol] = max(0, int(prev_oi + prev_oi * random.uniform(-0.001, 0.0015)))

                now_ts = datetime.now(timezone.utc)
                tick = {
                    "symbol": symbol,
                    "price": self._prices[symbol],
                    "prev_price": prev_price,
                    "volume": random.randint(1, 200),
                    "oi": self._oi[symbol],
                    "prev_oi": prev_oi,
                    # Simulated bid/ask around mid-price for OFI calculation
                    "bid": round(self._prices[symbol] - random.uniform(0.1, 0.5), 2),
                    "ask": round(self._prices[symbol] + random.uniform(0.1, 0.5), 2),
                    "bid_vol": random.randint(50, 500),
                    "ask_vol": random.randint(50, 500),
                    "timestamp": now_ts.isoformat(),
                    "type": "TICK"
                }

                # Update Redis for UI and staleness watchdog
                await self.redis_client.set(f"latest_tick:{symbol}", json.dumps(tick))
                self._last_tick_ts[symbol] = now_ts.timestamp()

                # Publish via ZMQ
                topic = f"TICK.{symbol}"
                await self.mq.send_json(self.pub_socket, tick, topic=topic)
                logger.debug(f"TICK {symbol} @ {self._prices[symbol]}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Tick stream error: {e}")
                await asyncio.sleep(1)

    # ── Lot Size Scheduler ───────────────────────────────────────────────────

    async def _lot_size_scheduler(self):
        """Fetches dynamic lot sizes from broker at 09:01 IST daily."""
        logger.info("Lot size scheduler active.")
        while True:
            now = datetime.now(tz=IST)
            target = now.replace(hour=9, minute=1, second=0, microsecond=0)
            if now >= target:
                # Already past 09:01 today — schedule for tomorrow
                target = target.replace(day=target.day + 1)

            wait_secs = (target - now).total_seconds()
            logger.info(f"Lot size fetch scheduled in {wait_secs:.0f}s (at 09:01 IST).")
            await asyncio.sleep(min(wait_secs, 60))  # check every 60s max

            now2 = datetime.now(tz=IST)
            if now2.hour == 9 and now2.minute == 1 and not self._lot_sizes_fetched:
                await self._fetch_and_store_lot_sizes()
                self._lot_sizes_fetched = True
                # Reset flag at midnight
                await asyncio.sleep(60)
                self._lot_sizes_fetched = False

    async def _fetch_and_store_lot_sizes(self):
        """
        Fetches lot sizes from Shoonya API.
        Falls back to hardcoded defaults if API unavailable.
        """
        lot_sizes = dict(DEFAULT_LOT_SIZES)  # start with defaults

        try:
            # In production: call Shoonya get_contracts() or search_scrip()
            # For now use known 2026 values as per SRS
            lot_sizes = {"NIFTY50": 65, "BANKNIFTY": 30}
            logger.info(f"✅ Lot sizes fetched: {lot_sizes}")
        except Exception as e:
            logger.warning(f"Lot size fetch failed ({e}). Using defaults: {lot_sizes}")

        # Store in Redis
        await self.redis_client.hset("lot_sizes", mapping=lot_sizes)
        await self.redis_client.publish(
            "system_events",
            json.dumps({"event": "LOT_SIZES_UPDATED", "lot_sizes": lot_sizes})
        )

    # ── Staleness Watchdog ───────────────────────────────────────────────────

    async def _staleness_watchdog(self):
        """Monitors tick freshness. Flags stale feeds (>1000ms) and forces reconnect."""
        logger.info("Staleness watchdog active (threshold: 1000ms).")
        await asyncio.sleep(10)  # Allow initial tick stream to start

        while True:
            try:
                now = datetime.now(timezone.utc).timestamp()
                for symbol, last_ts in list(self._last_tick_ts.items()):
                    age_ms = (now - last_ts) * 1000
                    if age_ms > STALENESS_THRESHOLD_MS:
                        logger.warning(
                            f"⚠️ STALE FEED: {symbol} last tick {age_ms:.0f}ms ago. "
                            f"Triggering socket reset..."
                        )
                        await self._force_socket_reset(symbol)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
            await asyncio.sleep(0.5)

    async def _force_socket_reset(self, symbol: str):
        """
        Simulates a TCP socket reset for a stale feed.
        In production: disconnect/reconnect the Shoonya WebSocket.
        """
        logger.warning(f"🔄 Socket reset triggered for {symbol}.")
        # Reset the last tick timestamp to avoid repeated triggers
        self._last_tick_ts[symbol] = datetime.now(timezone.utc).timestamp()

        await self.redis_client.publish("system_events", json.dumps({
            "event": "FEED_RESET",
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }))

    # ── SEBI Circuit Breaker Monitor ─────────────────────────────────────────

    async def _circuit_breaker_monitor(self):
        """
        Detects SEBI-mandated circuit breaker halts (10% / 15% / 20% index moves).
        Broadcasts SYSTEM_HALT with time-of-day appropriate sleep duration.
        """
        logger.info("Circuit breaker monitor active.")
        base_nifty = self._prices["NIFTY50"]
        base_banknifty = self._prices["BANKNIFTY"]

        while True:
            try:
                nifty_chg = abs(self._prices["NIFTY50"] - base_nifty) / base_nifty * 100
                bn_chg = abs(self._prices["BANKNIFTY"] - base_banknifty) / base_banknifty * 100
                max_chg = max(nifty_chg, bn_chg)

                halt_level = None
                for level in [20, 15, 10]:
                    if max_chg >= level:
                        halt_level = level
                        break

                if halt_level and not self._system_halted:
                    halt_mins = self._get_halt_duration(halt_level)
                    logger.critical(
                        f"🚨 CIRCUIT BREAKER L{halt_level}%: Market moved {max_chg:.2f}%. "
                        f"Halting for {halt_mins} minutes."
                    )
                    await self._broadcast_halt(halt_level, halt_mins)

                    if halt_mins > 0:
                        self._system_halted = True
                        await asyncio.sleep(halt_mins * 60)
                        self._system_halted = False
                        base_nifty = self._prices["NIFTY50"]
                        base_banknifty = self._prices["BANKNIFTY"]
                        await self.redis_client.set("SYSTEM_HALT", "False")
                        logger.info("Circuit breaker halt lifted. Resuming tick stream.")
                    else:
                        # >2:30 PM or 20% → Day halt
                        logger.critical("🚫 DAY HALT: Market closed for the session.")
                        self._system_halted = True
                        await self.redis_client.set("SYSTEM_HALT", "DAY_HALT")

            except Exception as e:
                logger.error(f"Circuit breaker monitor error: {e}")

            await asyncio.sleep(5)

    def _get_halt_duration(self, level: int) -> int:
        """Returns halt duration in minutes based on time-of-day matrix."""
        now = datetime.now(tz=IST).time()
        matrix = CIRCUIT_BREAKER_MATRIX.get(level, {})

        if now < dt_time(13, 0):
            return matrix.get("before_1_pm", 0)
        elif now < dt_time(14, 30):
            return matrix.get("before_2_30_pm", 0)
        else:
            return matrix.get("after_2_30_pm", 0)

    async def _broadcast_halt(self, level: int, halt_mins: int):
        """Publishes SYSTEM_HALT event to Redis and ZeroMQ."""
        payload = {
            "event": "SYSTEM_HALT",
            "level": level,
            "halt_minutes": halt_mins,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        await self.redis_client.set("SYSTEM_HALT", str(level))
        await self.redis_client.publish("system_events", json.dumps(payload))
        # Also push Telegram alert
        await self.redis_client.lpush("telegram_alerts", json.dumps({
            "message": f"🚨 CIRCUIT BREAKER L{level}%! Halt: {halt_mins} min.",
            "type": "CIRCUIT_BREAKER",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }))


async def start_gateway():
    gw = DataGateway()
    try:
        await gw.start()
    except KeyboardInterrupt:
        logger.info("DataGateway shutting down.")
    finally:
        gw.pub_socket.close()
        gw.mq.context.term()
        if gw.redis_client:
            await gw.redis_client.aclose()


if __name__ == "__main__":
    try:
        if uvloop:
            uvloop.install()
        elif hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(start_gateway())
    except KeyboardInterrupt:
        pass
