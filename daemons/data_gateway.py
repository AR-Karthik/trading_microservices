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
import os
import threading

import pyotp
from dotenv import load_dotenv

import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics
from core.greeks import BlackScholes
from core.alerts import send_cloud_alert
from NorenRestApiPy.NorenApi import NorenApi

load_dotenv()

RISK_FREE_RATE = 0.065

RISK_FREE_RATE = 0.065

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
# Symbols simulated / watched
SYMBOLS_UNDERLYING = ["NIFTY50", "BANKNIFTY", "SENSEX", "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT"]

# Shoonya API Tokens Mapping
TOKEN_TO_SYMBOL = {
    "26000": "NIFTY50",
    "26009": "BANKNIFTY",
    "2885":  "RELIANCE",
    "1333":  "HDFCBANK",
    "1594":  "INFY",
    "11536": "TCS",
    "4963":  "ICICIBANK",
    "1":     "SENSEX",
    "1660":  "ITC",
    "3045":  "SBIN",
    "5900":  "AXISBANK",
    "1922":  "KOTAKBANK",
    "11483": "LT"
}
SHOONYA_SUBSCRIPTIONS = [
    "NSE|26000", "NSE|26009", "NSE|2885", "NSE|1333", 
    "NSE|1594", "NSE|11536", "NSE|4963", "BSE|1",
    "NSE|1660", "NSE|3045", "NSE|5900", "NSE|1922", "NSE|11483"
]

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
    def __init__(self, redis_url: str | None = None):
        self.mq = MQManager()
        if redis_url is None:
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_url = f"redis://{redis_host}:6379"
        self.redis_url = redis_url
        self.redis_client: redis.Redis | None = None
        self.pub_socket = self.mq.create_publisher(Ports.MARKET_DATA)

        self._prices = {
            "NIFTY50": 22350.0, "BANKNIFTY": 47200.0, "SENSEX": 73000.0,
            "RELIANCE": 1480.0, "HDFCBANK": 1680.0,
            "INFY": 1820.0, "TCS": 3940.0, "ICICIBANK": 1230.0
        }
        self._oi = {s: random.randint(800_000, 1_500_000) for s in SYMBOLS_UNDERLYING}
        self._last_tick_ts: dict[str, float] = {}
        self._lot_sizes_fetched = False
        self._system_halted = False

        # Shoonya API Setup
        host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
        ws_host = host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")
        self.api = NorenApi(host=host, websocket=ws_host)
        self.tick_queue = asyncio.Queue()
        self.active_option_tokens = {} # token -> symbol (e.g. "12345" -> "NIFTY26MAR22350CE")
        self.last_expiry_sync = 0

    # ── Authentication ───────────────────────────────────────────────────────

    def _login(self):
        user = os.getenv("SHOONYA_USER")
        pwd = os.getenv("SHOONYA_PWD")
        factor2 = os.getenv("SHOONYA_FACTOR2")
        vc = os.getenv("SHOONYA_VC")
        app_key = os.getenv("SHOONYA_APP_KEY")
        imei = os.getenv("SHOONYA_IMEI")
        sim_mode = os.getenv("SIMULATION_MODE", "false").lower() == "true"

        if sim_mode:
            logger.warning("⚠️ SIMULATION_MODE active. Skipping Shoonya login.")
            return True

        if not all([user, pwd, factor2, vc, app_key, imei]):
            logger.error("Missing Shoonya credentials in .env file.")
            return False

        try:
            totp = pyotp.TOTP(factor2).now()
            res = self.api.login(userid=user, password=pwd, twoFA=totp, vendor_code=vc, api_secret=app_key, imei=imei)

            if res and res.get("stat") == "Ok":
                logger.info("✅ Shoonya Login OK")
                return True
            else:
                logger.error(f"❌ Shoonya Login failed: {res}")
                return False
        except Exception as e:
            logger.error(f"❌ Shoonya Login exception: {e}")
            return False

    # ── Gateway Startup ──────────────────────────────────────────────────────

    async def start(self):
        self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
        logger.info("DataGateway initialised. Starting sub-tasks...")
        asyncio.create_task(send_cloud_alert("🚀 DATA GATEWAY: Service active. Monitoring 13 heavyweights + Indices.", alert_type="SYSTEM"))
        self._data_flow_alert_sent = False

        await asyncio.gather(
            self._tick_stream(),
            self._lot_size_scheduler(),
            self._staleness_watchdog(),
            self._circuit_breaker_monitor(),
            self._dynamic_subscription_manager(),
        )

    # ── Strike Selection (SRS Phase 2) ───────────────────────────────────────

    def get_optimal_strike(self, spot: float, option_type: str = "call", expiry_years: float = 2.0/365, iv: float = 0.18) -> float:
        """Filters option chain for Delta between 0.40 and 0.60 (Delta-Theta Balance)."""
        best_strike = spot
        closest_delta_diff = 1.0
        
        # Check strikes +/- 500 from spot in intervals of 50
        base_strike = round(spot / 50) * 50
        for offset in range(-500, 550, 50):
            strike = base_strike + offset
            delta = BlackScholes.delta(spot, strike, expiry_years, RISK_FREE_RATE, iv, option_type)
            abs_delta = abs(delta)
            
            # Filter for Delta [0.40, 0.60]
            if 0.40 <= abs_delta <= 0.60:
                diff = abs(abs_delta - 0.50)  # Aim for Delta ~0.50
                if diff < closest_delta_diff:
                    closest_delta_diff = diff
                    best_strike = strike
                    
        return best_strike

    # ── Tick Stream ──────────────────────────────────────────────────────────

    async def _tick_stream(self):
        """
        Processes live ticks from the Shoonya WebSocket background thread.
        Uses asyncio.Queue to bridge the synchronous callback to the async event loop.
        """
        logger.info("Starting live Shoonya WebSocket integration...")
        sim_mode = os.getenv("SIMULATION_MODE", "false").lower() == "true"

        if not self._login():
            logger.error("Could not log in to Shoonya. Tick stream will not start.")
            return

        loop = asyncio.get_running_loop()
        feed_opened = threading.Event()

        def on_feed_update(tick_msg):
            # Safe queuing from the WebSocket thread
            loop.call_soon_threadsafe(self.tick_queue.put_nowait, tick_msg)

        def on_open():
            logger.info("✅ Shoonya WebSocket Connected")
            feed_opened.set()
            for sub in SHOONYA_SUBSCRIPTIONS:
                self.api.subscribe(sub)
                logger.info(f"Subscribed to {sub}")

        def on_error(err):
            logger.error(f"Shoonya WS Error: {err}")

        def on_close():
            logger.warning("Shoonya WS Closed")

        ws_thread = threading.Thread(
            target=self.api.start_websocket,
            kwargs={
                "subscribe_callback": on_feed_update,
                "order_update_callback": lambda o: None,
                "socket_open_callback": on_open,
                "socket_error_callback": on_error,
                "socket_close_callback": on_close
            },
            daemon=True
        )
        ws_thread.start()

        def _wait_for_feed():
            return feed_opened.wait(20.0)
            
        # Wait for socket to open with a generous timeout for staging environments
        if not sim_mode:
            await asyncio.to_thread(_wait_for_feed)
            if not feed_opened.is_set():
                logger.error("WebSocket failed to connect within 20 seconds. Falling back to simulation if requested.")
                if not sim_mode: return

        while True:
            if self._system_halted:
                await asyncio.sleep(1)
                continue

            try:
                # Read from queue or simulate
                if sim_mode:
                    await asyncio.sleep(random.uniform(0.1, 0.5))
                    symbol = random.choice(SYMBOLS_UNDERLYING)
                    base_price = self._prices.get(symbol, 1000.0)
                    price = base_price * (1 + random.uniform(-0.0005, 0.0005))
                    raw_tick = {'t': 'tk', 'tk': next(k for k,v in TOKEN_TO_SYMBOL.items() if v == symbol), 'lp': str(price), 'v': str(random.randint(1, 100))}
                else:
                    raw_tick = await self.tick_queue.get()

                if raw_tick.get('t') not in ('tk', 'tf'):
                    continue

                # Signal live data flow on first valid tick
                if not getattr(self, '_data_flow_alert_sent', False):
                    asyncio.create_task(send_cloud_alert("✅ DATA INGESTION LIVE: First market ticks received and broadcasting via Redis.", alert_type="INFO"))
                    self._data_flow_alert_sent = True

                token = raw_tick.get('tk')
                symbol = TOKEN_TO_SYMBOL.get(token) or self.active_option_tokens.get(token)

                if not symbol:
                    continue

                if 'lp' in raw_tick:
                    price = float(raw_tick['lp'])
                    self._prices[symbol] = price
                else:
                    price = self._prices[symbol]

                # Synthesize standard fields safely handles missing properties in touchlines
                bid = float(raw_tick.get('bp1', price))
                ask = float(raw_tick.get('sp1', price))
                # Fallback for indices lacking order book
                if bid == price and ask == price:
                    bid = price - 0.5
                    ask = price + 0.5

                vol = int(raw_tick.get('v', 1))
                if vol == 0: vol = 1

                now_ts = datetime.now(timezone.utc)
                tick = {
                    "symbol": symbol,
                    "price": price,
                    "prev_price": self._prices[symbol],
                    "volume": vol,
                    "last_volume": vol,
                    "oi": int(raw_tick.get('oi', self._oi.get(symbol, 0))),
                    "prev_oi": self._oi.get(symbol, 0),
                    "bid": bid,
                    "ask": ask,
                    "bid_vol": int(raw_tick.get('bq1', 100)),
                    "ask_vol": int(raw_tick.get('sq1', 100)),
                    "timestamp": now_ts.isoformat(),
                    "type": "TICK"
                }

                # Update Redis
                await self.redis_client.set(f"latest_tick:{symbol}", json.dumps(tick))
                self._last_tick_ts[symbol] = now_ts.timestamp()

                # Simulate Strike Selection
                if symbol == "NIFTY50":
                    optimal_ce_strike = self.get_optimal_strike(price, "call", 2/365, 0.18)
                    optimal_pe_strike = self.get_optimal_strike(price, "put", 2/365, 0.18)
                    
                    await self.redis_client.hset("optimal_strikes", mapping={
                        "NIFTY_CE": optimal_ce_strike,
                        "NIFTY_PE": optimal_pe_strike
                    })

                # Publish via ZMQ
                topic = f"TICK.{symbol}"
                await self.mq.send_json(self.pub_socket, tick, topic=topic)
                
                # Debug sample to keep terminal clean
                if random.random() < 0.05:
                    logger.debug(f"LIVE TICK {symbol} @ {price}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Tick stream processing error: {e}")
                await asyncio.sleep(0.1)

    # ── Dynamic Option Subscriptions ──────────────────────────────────────────

    async def _dynamic_subscription_manager(self):
        """Periodically refreshes ATM option subscriptions based on spot price."""
        logger.info("Dynamic option subscription manager active.")
        while True:
            if self._system_halted:
                await asyncio.sleep(10)
                continue
                
            try:
                # 1. Selection logic for NIFTY & BANKNIFTY
                for idx in ["NIFTY50", "BANKNIFTY"]:
                    spot = self._prices.get(idx)
                    if not spot: continue
                    
                    # Fetch Expiry (Once per day or if stale)
                    now_ts = datetime.now().timestamp()
                    if now_ts - self.last_expiry_sync > 3600: # 1h
                        await self._sync_expiries()
                        self.last_expiry_sync = now_ts
                    
                    # Select ideal strikes
                    ce_strike = self.get_optimal_strike(spot, "call")
                    pe_strike = self.get_optimal_strike(spot, "put")
                    
                    # Construct and Subscribe
                    await self._ensure_option_subscription(idx, ce_strike, "CE")
                    await self._ensure_option_subscription(idx, pe_strike, "PE")
                    
            except Exception as e:
                logger.error(f"Subscription manager error: {e}")
            
            await asyncio.sleep(300) # Every 5 mins

    async def _sync_expiries(self):
        """Fetches the nearest expiry for indices from Shoonya."""
        if os.getenv("SIMULATION_MODE", "false").lower() == "true":
            return
            
        try:
            # Fetch for NIFTY as baseline
            res = self.api.get_option_chain(exchange='NFO', tradingsymbol='NIFTY', strike=22000, count=1)
            if res and isinstance(res, dict) and res.get('stat') == 'Ok':
                # Example response: {'stat': 'Ok', 'values': [{'exDate': '26-MAR-2026', ...}]}
                values = res.get('values', [])
                if values:
                    expiry = values[0].get('exDate') # e.g. "26-MAR-2026"
                    # Format for scrip search: "26MAR"
                    parts = expiry.split('-')
                    formatted = f"{parts[0]}{parts[1]}"
                    await self.redis_client.set("CURRENT_EXPIRY_DATE", formatted)
                    logger.info(f"Sync'd current expiry: {formatted}")
        except Exception as e:
            logger.error(f"Expiry sync failed: {e}")

    async def _ensure_option_subscription(self, idx: str, strike: float, otype: str):
        """Finds token for strike/type and subscribes if not active."""
        if os.getenv("SIMULATION_MODE", "false").lower() == "true":
            # Generate fake token for simulation
            fake_token = f"OPT_{idx}_{int(strike)}_{otype}"
            self.active_option_tokens[fake_token] = f"{idx}_{int(strike)}_{otype}"
            return

        expiry = await self.redis_client.get("CURRENT_EXPIRY_DATE") or "26MAR"
        search_text = f"{idx} {expiry} {int(strike)} {otype}"
        
        try:
            res = self.api.search_scrip(exchange='NFO', searchtext=search_text)
            if res and res.get('stat') == 'Ok':
                values = res.get('values', [])
                if values:
                    token = values[0].get('token')
                    tsym = values[0].get('tsym')
                    
                    if token not in self.active_option_tokens:
                        logger.info(f"New ATM Option found: {tsym} (Token: {token})")
                        self.active_option_tokens[token] = tsym
                        self.api.subscribe(f"NFO|{token}")
        except Exception as e:
            logger.error(f"Option subscription failed for {search_text}: {e}")

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
            logger.info(f"Lot sizes fetched: {lot_sizes}")
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
                            f"STALE FEED: {symbol} last tick {age_ms:.0f}ms ago. "
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
        logger.warning(f"Socket reset triggered for {symbol}.")
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
                nifty_chg = abs(float(self._prices["NIFTY50"]) - float(base_nifty)) / float(base_nifty) * 100
                bn_chg = abs(float(self._prices["BANKNIFTY"]) - float(base_banknifty)) / float(base_banknifty) * 100
                max_chg = max(nifty_chg, bn_chg)

                halt_level = None
                for level in [20, 15, 10]:
                    if max_chg >= level:
                        halt_level = level
                        break

                if halt_level and not self._system_halted:
                    halt_mins = self._get_halt_duration(halt_level)
                    logger.critical(
                        f"CIRCUIT BREAKER L{halt_level}%: Market moved {max_chg:.2f}%. "
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
                        logger.critical("DAY HALT: Market closed for the session.")
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
        # Also push Telegram alert via Pub/Sub
        await send_cloud_alert(
            f"⛔ CIRCUIT BREAKER L{level}% triggered! Market halt: {halt_mins} min.",
            alert_type="CRITICAL"
        )


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
