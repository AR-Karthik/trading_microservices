"""
daemons/tick_sensor.py
======================
Fast Lane: Pure WebSocket ingestion for Shoonya API.
Pinned to a dedicated CPU core for zero-jitter performance.
"""

import asyncio
import json
import logging
import os
import sys
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pyotp
import redis.asyncio as redis
import zmq # [Parity]
from dotenv import load_dotenv

try:
    import uvloop
except ImportError:
    uvloop = None

from core.mq import MQManager, Ports, Topics, NumpyEncoder
from core.alerts import send_cloud_alert
from core.shared_memory import TickSharedMemory
from NorenRestApiPy.NorenApi import NorenApi

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("TickSensor")

IST = ZoneInfo("Asia/Kolkata")

# Symbols & Tokens (Shared with SnapshotManager via Redis/Config)
TOKEN_TO_SYMBOL = {
    "26000": "NIFTY50", "26009": "BANKNIFTY", "2885": "RELIANCE",
    "1333": "HDFCBANK", "1594": "INFY", "11536": "TCS",
    "4963": "ICICIBANK", "1": "SENSEX", "1660": "ITC",
    "3045": "SBIN", "5900": "AXISBANK", "1922": "KOTAKBANK", "11483": "LT",
    "26017": "INDIAVIX"
}

# [Parity] Asset-specific tick sizes and ratios
TICK_SIZES = {"NIFTY50": 0.05, "BANKNIFTY": 0.05, "SENSEX": 1.00, "DEFAULT": 0.05}
TICK_RATIOS = {"NIFTY50": 1.0, "BANKNIFTY": 0.5, "SENSEX": 0.25, "DEFAULT": 1.0}

# [Parity] SHM Slot Mapping
SYMBOL_TO_SLOT = {s: i for i, s in enumerate(TOKEN_TO_SYMBOL.values())}
INITIAL_SUBSCRIPTIONS = [f"NSE|{k}" if k != "1" else f"BSE|{k}" for k in TOKEN_TO_SYMBOL.keys()]

class TickSensor:
    def __init__(self):
        self.mq = MQManager()
        self.pub_socket = self.mq.create_publisher(Ports.MARKET_DATA)
        self.redis_client = None
        self.tick_queue = asyncio.Queue()
        self.api = None
        self.active_tokens = dict(TOKEN_TO_SYMBOL)
        self._ws_stopped = threading.Event()
        self._ws_reconnect_flag = False
        self._last_stale_alert_ts = 0  # [Parity]
        self._last_tick_ts = {}        # [Parity]
        self._prices = {}              # [Parity]
        self._oi = {}                  # [Parity]
        self._data_flow_alert_sent = False
        self._sim_ltt_counter = 0      # [Audit-Fix] Persistent LTT for simulation
        
        self.sim_mode = os.getenv("SIMULATION_MODE", "false").lower() == "true"
        self.off_hour_sim = os.getenv("ENABLE_OFF_HOUR_SIMULATOR", "false").lower() == "true"

        # [Parity] Initialize Tick SHM segment
        try:
            self.shm_ticks = TickSharedMemory(create=True)
            logger.info("✅ Tick Shared Memory segment created (Fast Lane).")
        except Exception as e:
            logger.error(f"❌ Failed to create Tick SHM: {e}")
            self.shm_ticks = None

    async def start(self):
        from core.auth import get_redis_url
        self.redis_client = redis.from_url(get_redis_url(), decode_responses=True)
        await self.redis_client.ping()
        
        # [Parity] Seed Tick Constants
        await self.redis_client.hset("tick_sizes", mapping=TICK_SIZES)
        await self.redis_client.hset("tick_ratios", mapping=TICK_RATIOS)
        
        logger.info("🚀 TickSensor (Fast Lane) active. Core pinning applied via Docker.")
        
        tasks = [
            self._tick_iterator(),
            self._subscription_listener(),
            self._reconnect_watchdog(),
            self._run_simulator()       # [Audit-Fix]
        ]
        await asyncio.gather(*tasks)

    async def _tick_iterator(self):
        """Main loop for Shoonya WebSocket connection and ingestion."""
        loop = asyncio.get_running_loop()

        def on_tick(raw):
            # [ROBUSTNESS] Multi-copy to avoid NorenApi object reuse bugs
            loop.call_soon_threadsafe(self.tick_queue.put_nowait, raw.copy())

        def on_open():
            logger.info("✅ Shoonya WS Connected (Fast Lane)")
            for sub in INITIAL_SUBSCRIPTIONS:
                self.api.subscribe(sub)
            
            # Subscribe to INDIA VIX specifically
            self.api.subscribe("NSE|26017")

        while True:
            if not self.sim_mode and not self.off_hour_sim:
                # Login and Start WS
                success = await self._connect_shoonya(on_tick, on_open)
                if not success:
                    await asyncio.sleep(10)
                    continue

            # Ingest loop
            while not self._ws_reconnect_flag:
                try:
                    raw = await asyncio.wait_for(self.tick_queue.get(), timeout=1.0)
                    await self._process_raw_tick(raw)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"Tick processing error: {e}")

            # Reconnect trigger
            self._ws_reconnect_flag = False
            await asyncio.sleep(5)

    async def _connect_shoonya(self, on_tick, on_open):
        """Connects to Shoonya with exponential backoff on login failures."""
        max_retries = 5
        base_delay = 5
        for attempt in range(max_retries):
            try:
                host = os.getenv("SHOONYA_HOST")
                ws_host = os.getenv("SHOONYA_WEBSOCKET_HOST")
                self.api = NorenApi(host=host, websocket=ws_host)
                
                totp = pyotp.TOTP(os.getenv("SHOONYA_FACTOR2")).now()
                ret = await asyncio.to_thread(self.api.login, 
                    userid=os.getenv("SHOONYA_USER"), password=os.getenv("SHOONYA_PWD"),
                    twoFA=totp, vendor_code=os.getenv("SHOONYA_VC"),
                    api_secret=os.getenv("SHOONYA_APP_KEY"), imei=os.getenv("SHOONYA_IMEI")
                )
                
                if ret and ret.get('stat') == 'Ok':
                    threading.Thread(target=self.api.start_websocket, 
                                     kwargs={"subscribe_callback": on_tick, "socket_open_callback": on_open},
                                     daemon=True).start()
                    return True
                
                logger.warning(f"Login failed (Attempt {attempt+1}/{max_retries}): {ret.get('emsg', 'Unknown error')}")
            except Exception as e:
                logger.error(f"Shoonya connection error (Attempt {attempt+1}/{max_retries}): {e}")
            
            await asyncio.sleep(min(base_delay * (2 ** attempt), 60))
        return False

    async def _process_raw_tick(self, raw):
        """Enhanced parsing for Shoonya ticks."""
        token = str(raw.get('tk'))
        symbol = self.active_tokens.get(token)
        if not symbol: return

        # Raw Price & Volume
        price = float(raw.get('lp', self._prices.get(symbol, 0.0)))
        prev_price = self._prices.get(symbol, price)
        self._prices[symbol] = price
        
        curr_oi = int(raw.get('oi', self._oi.get(symbol, 0)))
        prev_oi = self._oi.get(symbol, curr_oi)
        self._oi[symbol] = curr_oi

        # [Parity] Initial Data Flow Alert
        if not self._data_flow_alert_sent:
            source = "LIVE" # TickSensor is pure live (WS)
            asyncio.create_task(send_cloud_alert(f"✅ DATA INGESTION {source}: Market ticks active.", alert_type="INFO"))
            self._data_flow_alert_sent = True

        # [Parity] Exchange Timestamp
        exchange_ts = raw.get('ft') or raw.get('ltt') or datetime.now().strftime("%H:%M:%S")

        tick = {
            "symbol": symbol,
            "price": price,
            "prev_price": prev_price,
            "volume": int(raw.get('v', 0)),
            "oi": curr_oi,
            "prev_oi": prev_oi,
            "total_buy_qty": int(raw.get('tb', 0)),
            "total_sell_qty": int(raw.get('ts', 0)),
            "exchange_ts": exchange_ts,
            "bid": float(raw.get('bp1', price)),
            "ask": float(raw.get('sp1', price)),
            "bid_vol": int(raw.get('bq1', 0)),
            "ask_vol": int(raw.get('sq1', 0)),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "WS",
            "type": "TICK"
        }

        # [Parity] Support for legacy PCR/Pressure Gauge keys
        oi_diff = tick["oi"] - tick["prev_oi"]
        if abs(oi_diff) > 500:
            tick["oi_accel"] = float(oi_diff)
            await self.redis_client.set(f"OI_ACCEL:{symbol}", str(oi_diff))
        else:
            tick["oi_accel"] = 0.0

        await self.redis_client.setnx(f"DAY_OPEN:{symbol}", str(price))

        if " CE" in symbol:
            base = symbol.split()[0]
            await self.redis_client.set(f"OI:CE:{base}", str(curr_oi))
        elif " PE" in symbol:
            base = symbol.split()[0]
            await self.redis_client.set(f"OI:PE:{base}", str(curr_oi))

        # Publish to ZMQ (Global and Per-Symbol Topics)
        await self.mq.send_json(self.pub_socket, Topics.TICK_DATA, tick)
        await self.mq.send_json(self.pub_socket, f"TICK.{symbol}", tick)
        
        # [Parity] Fast Cache in Redis (Latest + History List)
        async with self.redis_client.pipeline(transaction=True) as pipe:
            pipe.set(f"latest_tick:{symbol}", json.dumps(tick, cls=NumpyEncoder))
            history_key = f"tick_history:{symbol}"
            pipe.rpush(history_key, json.dumps(tick, cls=NumpyEncoder))
            pipe.ltrim(history_key, -2000, -1)
            await pipe.execute()

        # [Parity] Update last tick time for Watchdog
        self._last_tick_ts[symbol] = time.time()

        # [Parity] Write to Shared Memory
        if self.shm_ticks and symbol in SYMBOL_TO_SLOT:

        # [Parity] Write to Shared Memory for Ultra-Low Latency consumers
        if self.shm_ticks and symbol in SYMBOL_TO_SLOT:
            slot = SYMBOL_TO_SLOT[symbol]
            ts_float = datetime.fromisoformat(tick["timestamp"]).timestamp()
            self.shm_ticks.write_tick(slot, symbol, price, volume, ts_float)

    async def _subscription_listener(self):
        """Listens for dynamic subscription requests from SnapshotManager."""
        pubsub = self.redis_client.pubsub()
        await pubsub.subscribe("tick_sensor:subscriptions")
        logger.info("Subscription listener active on 'tick_sensor:subscriptions'.")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    # data format: {"action": "subscribe", "symbol": "NSE|12345", "tsym": "NIFTY..."}
                    if data["action"] == "subscribe":
                        exch_token = data["symbol"]
                        tsym = data["tsym"]
                        token = exch_token.split("|")[1]
                        self.active_tokens[token] = tsym
                        if self.api:
                            self.api.subscribe(exch_token)
                            logger.info(f"⚡ JIT Subscribed: {tsym} ({exch_token})")
                except Exception as e:
                    logger.error(f"Subscription listener error: {e}")

    async def _reconnect_watchdog(self):
        """[Parity] Monolithic Watchdog: Throttled alerts, silent resets, and SYSTEM_EVENTS."""
        logger.info("Staleness watchdog active (threshold: 10s).")
        await asyncio.sleep(10)
        
        while True:
            try:
                now = time.time()
                for symbol, last_ts in list(self._last_tick_ts.items()):
                    age_ms = (now - last_ts) * 1000
                    if age_ms > 10000: # 10s threshold
                        if symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                            if (now - self._last_stale_alert_ts) > 30: # 30s throttle
                                logger.warning(f"🚨 FEED STALL: {symbol} is {age_ms/1000:.0f}s stale!")
                                await self._force_socket_reset(symbol, silent=False)
                                if not self.sim_mode:
                                    asyncio.create_task(send_cloud_alert(f"⚠️ FEED STALL: {symbol} (>{age_ms/1000:.0f}s). Resetting...", alert_type="WARNING"))
                                self._last_stale_alert_ts = now
                        else:
                            await self._force_socket_reset(symbol, silent=True)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
            await asyncio.sleep(1)

    async def _force_socket_reset(self, symbol: str, silent: bool = False):
        """[Parity] Forces a full WS restart and publishes FEED_RESET event."""
        if not silent:
            logger.warning(f"🔄 Socket reset triggered for {symbol}.")
        self._ws_reconnect_flag = True
        try:
            if self.api: await asyncio.to_thread(self.api.close_websocket)
        except: pass
        self._last_tick_ts[symbol] = time.time()
        
        await self.redis_client.publish("system_events", json.dumps({
            "event": "FEED_RESET", "symbol": symbol, "timestamp": datetime.now(timezone.utc).isoformat(), "silent": silent
        }))

    async def _run_simulator(self):
        """[Audit-Fix] High-Frequency Off-Hour Simulator for Fast Lane parity."""
        if not self.off_hour_sim and not self.sim_mode: return
        logger.info("🚀 Fast-Lane Simulator active (10Hz Brownian Mode).")
        
        DRIFT = 0.000005 
        VOL = 0.0001
        
        while True:
            try:
                self._sim_ltt_counter += 1
                now = datetime.now(tz=IST)
                sim_ltt = (datetime.combine(now.date(), datetime.min.time()) + 
                           (now - datetime.combine(now.date(), datetime.min.time()))).strftime("%H:%M:%S")
                sim_ft = time.time()

                for sym in ["NIFTY50", "BANKNIFTY"]:
                    base = self._prices.get(sym, 22000.0 if sym == "NIFTY50" else 48000.0)
                    price = base * (1 + random.gauss(DRIFT, VOL))
                    prev_price = self._prices.get(sym, price)
                    self._prices[sym] = price
                    
                    tick = {
                        "symbol": sym, "price": round(price, 2), "prev_price": round(prev_price, 2),
                        "volume": random.randint(100, 500), "oi": random.randint(100000, 500000),
                        "exchange_ts": sim_ltt, "ltt": sim_ltt, "ft": sim_ft,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": "SIM", "type": "TICK"
                    }
                    # Fast Lane: Local SHM + ZMQ only to reduce Redis pressure
                    if self.shm_ticks and sym in SYMBOL_TO_SLOT:
                        self.shm_ticks.write_tick(SYMBOL_TO_SLOT[sym], sym, price, tick["volume"], sim_ft)
                    
                    await self.mq.send_json(self.pub_socket, Topics.TICK_DATA, tick)
                    
                await asyncio.sleep(0.1) # 10Hz for Fast Lane feel
            except Exception as e:
                logger.error(f"TickSim error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    if uvloop: uvloop.install()
    elif hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sensor = TickSensor()
    asyncio.run(sensor.start())
