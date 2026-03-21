"""
Real-Time Market Data Gateway
Orchestrates high-frequency tick ingestion from broker WebSockets.
Manages dynamic option subscriptions, staleness monitoring, and ZMQ broadcasting.
"""

import asyncio
import json
import logging
import random
import sys
import os
import zmq
import zmq.asyncio
from datetime import datetime, timezone, time as dt_time
from zoneinfo import ZoneInfo
from core.shm import ShmManager
from core.shared_memory import TickSharedMemory
import os
import threading
import time

import pyotp
from dotenv import load_dotenv

import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics, NumpyEncoder  # [F1-04] Added NumpyEncoder
from core.greeks import BlackScholes
from core.alerts import send_cloud_alert
from core.network_utils import exponential_backoff
from NorenRestApiPy.NorenApi import NorenApi

load_dotenv()

# RISK_FREE_RATE now dynamic (Audit 5.1)

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
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

def is_market_hours():
    now_dt = datetime.now(IST)
    now = now_dt.time()
    # Market is open from 09:15 to 15:30 IST on weekdays
    is_weekday = now_dt.weekday() < 5
    res = is_weekday and (MARKET_OPEN <= now <= MARKET_CLOSE)
    
    # Extra logging to debug why we might be in simulated mode
    if random.random() < 0.01: # Sample logging to avoid spam
        logger.info(f"DEBUG: Current IST Time: {now_dt.strftime('%H:%M:%S')}. Market Open: {res}")
    
    return res

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
# [F5-01] Updated defaults to match spec (Nifty=65, BankNifty=30)
DEFAULT_LOT_SIZES = {"NIFTY50": 65, "BANKNIFTY": 30, "SENSEX": 10, "RELIANCE": 250, "HDFCBANK": 550, "ICICIBANK": 700, "INFY": 400, "TCS": 175, "ITC": 1600, "SBIN": 1500, "AXISBANK": 625, "KOTAKBANK": 400, "LT": 300}

# Asset-specific tick sizes and ratios
TICK_SIZES = {
    "NIFTY50": 0.05,
    "BANKNIFTY": 0.05,
    "SENSEX": 1.00, # BSE index options tick size
    "DEFAULT": 0.05
}

TICK_RATIOS = {
    "NIFTY50": 1.0, # Normalizes movements relative to each other if cross-index calc is needed
    "BANKNIFTY": 0.5,
    "SENSEX": 0.25,
    "DEFAULT": 1.0
}

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
            from core.auth import get_redis_url
            self.redis_url = get_redis_url()
        else:
            self.redis_url = redis_url
        self.redis_client: redis.Redis | None = None
        self.pub_socket = self.mq.create_publisher(Ports.MARKET_DATA)

        # [Audit 9.2] Internal structures
        self._prices = {}
        self._oi = {s: random.randint(800_000, 1_500_000) for s in SYMBOLS_UNDERLYING}
        self._last_tick_ts = {}
        self._system_halted = False
        self._data_flow_alert_sent = False
        self._lot_sizes_fetched = False
        self.sim_mode = False  # Dynamic state
        self._last_stale_alert_ts = 0.0  # Wave 3.9: Alert throttle

        # Shoonya API Setup
        host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
        ws_host = host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP/")
        self.api = NorenApi(host=host, websocket=ws_host)
        self.tick_queue = asyncio.Queue()
        self.active_option_tokens = {} # token -> symbol (e.g. "12345" -> "NIFTY26MAR22350CE")
        self.last_expiry_sync: float = 0.0
        
        # [Audit] WebSocket state management
        self._ws_stopped = threading.Event()
        self._ws_reconnect_flag = False 
        
        # Simulation Settings
        self.sim_mode = os.getenv("SIMULATION_MODE", "false").lower() == "true"
        self.off_hour_sim = os.getenv("ENABLE_OFF_HOUR_SIMULATOR", "false").lower() == "true"
        
        # [Audit] Create the Tick SHM segment once on startup
        try:
            self.shm_ticks = TickSharedMemory(create=True)
            
            # [Audit-Fix] Initialize history fetch flag
            self._history_synced = False
            logger.info(f"✅ Tick Shared Memory segment created. SimMode: {self.sim_mode}, OffHourSim: {self.off_hour_sim}")
        except Exception as e:
            logger.error(f"❌ Failed to create Tick Shared Memory: {e}")
            self.shm_ticks = None

    # ── Authentication ───────────────────────────────────────────────────────

    def _login(self):
        user = os.getenv("SHOONYA_USER")
        pwd = os.getenv("SHOONYA_PWD")
        factor2 = os.getenv("SHOONYA_FACTOR2")
        vc = os.getenv("SHOONYA_VC")
        app_key = os.getenv("SHOONYA_APP_KEY")
        imei = os.getenv("SHOONYA_IMEI")

        if self.sim_mode:
            logger.warning("⚠️ SIMULATION_MODE active. Skipping Shoonya login.")
            return True

        if not all([user, pwd, factor2, vc, app_key, imei]):
            logger.error("Missing Shoonya credentials in .env file.")
            return False

        if not factor2:
            logger.error("❌ Shoonya TOTP secret is empty. Cannot generate OTP.")
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

    @exponential_backoff(max_retries=10, base_delay=5)
    async def _ensure_login(self):
        """Ensures login succeeds with retries."""
        if self._login():
            return True
        raise Exception("Shoonya Login Failed")

    # ── Gateway Startup ──────────────────────────────────────────────────────

    async def start(self):
        # [Audit 14.3] Added Redis connection retry loop
        retry_count = 0
        while True:
            try:
                from core.auth import get_redis_url
                self.redis_client = redis.from_url(
                    self.redis_url or get_redis_url(), 
                    decode_responses=True
                )
                await self.redis_client.ping()
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"Redis connection failed (Attempt {retry_count}): {e}")
                await asyncio.sleep(min(5 * retry_count, 60))

        logger.info("DataGateway initialised. Starting sub-tasks...")
        asyncio.create_task(send_cloud_alert("🚀 DATA GATEWAY: Service active. Monitoring 13 heavyweights + Indices.", alert_type="SYSTEM"))
        self._data_flow_alert_sent = False

        try:
            # Concurrent Tasks [Audit 14.1: Centralized History Sync]
            tasks = [
                self._tick_stream(),
                self._mode_controller(),
                self._lot_size_scheduler(),
                self._config_update_listener(),
                self._staleness_watchdog(),
                self._dynamic_subscription_manager(),
                self._dynamic_subscription_listener(), # Phase 6
                self._pcr_ingestion_loop(),
                self._circuit_breaker_monitor(),
                self._history_sync_scheduler(),
                self._run_simulator(),
                self._run_heartbeat(),
            ]
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.critical(f"🛑 FATAL: DataGateway sub-task failed: {e}", exc_info=True)
            raise

    async def _run_heartbeat(self):
        from core.health import HeartbeatProvider
        hb = HeartbeatProvider("DataGateway", self.redis_client)
        await hb.run_heartbeat()

    # ── Strike Selection (SRS Phase 2) ───────────────────────────────────────

    async def get_optimal_strike(self, spot: float, option_type: str = "call", expiry_years: float = 2.0/365, iv: float = 0.18) -> float:
        """Filters option chain for Delta between 0.40 and 0.60 (Delta-Theta Balance)."""
        best_strike = spot
        closest_delta_diff = 1.0
        
        # Check strikes +/- 500 from spot in intervals of 50
        base_strike = round(spot / 50) * 50
        # Audit 5.1: Dynamic Risk-Free Rate
        r = float(await self.redis_client.get("CONFIG:RISK_FREE_RATE") or 0.065)

        for offset in range(-500, 550, 50):
            strike = base_strike + offset
            delta = BlackScholes.delta(spot, strike, expiry_years, r, iv, option_type)
            abs_delta = abs(delta)
            
            # Filter for Delta [0.40, 0.60]
            if 0.40 <= abs_delta <= 0.60:
                diff = abs(abs_delta - 0.50)  # Aim for Delta ~0.50
                if diff < closest_delta_diff:
                    closest_delta_diff = diff
                    best_strike = strike
                    
        return best_strike

    # ── Dynamic Mode Controller ──────────────────────────────────────────────

    async def _mode_controller(self):
        """Monitors market hours and connectivity to switch between LIVE and SIMULATED."""
        logger.info("Dynamic Mode Controller active.")
        while True:
            try:
                sim_enabled = os.getenv("ENABLE_OFF_HOUR_SIMULATOR", "true").lower() == "true"
                sim_mode_global = os.getenv("SIMULATION_MODE", "false").lower() == "true"
                market_on = is_market_hours()
                
                new_sim_mode = self.sim_mode
                
                if market_on:
                    # In market hours, follow the global setting
                    new_sim_mode = sim_mode_global
                else:
                    # Off-hours: switch to simulated if enabled
                    new_sim_mode = sim_enabled

                if new_sim_mode != self.sim_mode:
                    old_mode = "SIMULATED" if self.sim_mode else "LIVE"
                    new_mode = "SIMULATED" if new_sim_mode else "LIVE"
                    reason = f"Market: {'ON' if market_on else 'OFF'}, GlobalSim: {sim_mode_global}"
                    logger.info(f"🔄 Mode Transition: {old_mode} -> {new_mode} ({reason})")
                    
                    self.sim_mode = new_sim_mode
                    self._data_flow_alert_sent = False 
                    
                    asyncio.create_task(send_cloud_alert(
                        f"🔄 DATA GATEWAY: Switch from {old_mode} to {new_mode} ({reason}).",
                        alert_type="SYSTEM"
                    ))
                    
            except Exception as e:
                logger.error(f"Mode controller error: {e}")
            
            await asyncio.sleep(60) # Check every minute

    # ── JIT WebSocket Subscriptions (Phase 6) ────────────────────────────────

    async def _dynamic_subscription_listener(self):
        """Listens for on-demand subscription requests from other daemons."""
        pubsub = self.redis_client.pubsub()
        await pubsub.subscribe("dynamic_subscriptions")
        logger.info("JIT Subscription Listener active on channel 'dynamic_subscriptions'.")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    symbol_data = message["data"] # "EXCH|TOKEN" or "NSE|26000"
                    if not symbol_data or "|" not in symbol_data: continue
                    
                    logger.info(f"⚡ JIT Subscription Request: {symbol_data}")
                    self.api.subscribe(symbol_data)
                    
                    # If it's an option symbol from a specific exchange
                    if symbol_data.startswith("NFO|"):
                        token = symbol_data.split("|")[1]
                        # We might need to fetch the tradingsymbol if it's not known
                        # and update self.active_option_tokens
                        # For now, we assume the requester handles symbol resolution
                        pass
                except Exception as e:
                    logger.error(f"JIT sub listener error: {e}")

    # ── Tick Stream ──────────────────────────────────────────────────────────

    async def _tick_stream(self):
        """
        Processes ticks from either the Shoonya WebSocket or a simulator.
        """
        logger.info("Starting tick stream manager...")
        loop = asyncio.get_running_loop()
        ws_thread = None
        feed_opened = threading.Event()

        def on_tick(tick_msg):
            # Final heartbeat log to confirm flow
            if tick_msg.get('tk') == '26000': 
                logger.debug(f"Nifty 50 Tick Ingested: {tick_msg.get('lp')}")
            # [Audit-Fix] MUST copy tick_msg as NorenApi reuses the same object
            loop.call_soon_threadsafe(self.tick_queue.put_nowait, tick_msg.copy())

        def on_open():
            logger.info("✅ Shoonya WebSocket Connected")
            try:
                limits = self.api.get_limits()
                logger.info(f"Shoonya Session Limits: {limits.get('stat') if isinstance(limits, dict) else 'Unknown'}")
            except Exception as e:
                logger.error(f"Error getting limits: {e}")
                
            feed_opened.set()
            for sub in SHOONYA_SUBSCRIPTIONS:
                self.api.subscribe(sub)

        def on_error(err):
            logger.error(f"❌ Shoonya WS Error: {err}")

        def on_close():
            logger.warning("Shoonya WS Closed.")
            self._ws_stopped.set()

        while True:
            if self._system_halted:
                await asyncio.sleep(1)
                continue

            try:
                # 1. Manage WebSocket Connection (Live Mode Only)
                # Skip if in hard SIMULATION_MODE or if OFF_HOUR_SIM is active
                if not self.sim_mode and not self.off_hour_sim:
                    if ws_thread is None or self._ws_stopped.is_set() or self._ws_reconnect_flag:
                        self._ws_reconnect_flag = False
                        host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP/")
                        ws_host = os.getenv("SHOONYA_WEBSOCKET_HOST") or host.replace("https", "wss").replace("NorenWClientTP", "NorenWSTP")
                        
                        logger.info(f"DEBUG: Attempting Shoonya Login for {os.getenv('SHOONYA_USER')}...")
                        logger.info(f"Connecting to WS: {ws_host}")
                        self.api = NorenApi(host=host, websocket=ws_host)
                        totp = pyotp.TOTP(os.getenv("SHOONYA_FACTOR2"))
                        twofa = totp.now()
                        
                        ret = await asyncio.to_thread(self.api.login, 
                            userid=os.getenv("SHOONYA_USER"), password=os.getenv("SHOONYA_PWD"),
                            twoFA=twofa, vendor_code=os.getenv("SHOONYA_VC"),
                            api_secret=os.getenv("SHOONYA_APP_KEY"), imei=os.getenv("SHOONYA_IMEI")
                        )
                        logger.info(f"DEBUG: Shoonya Login Response: {ret}")
                        
                        if ret and ret.get('stat') == 'Ok':
                            logger.info("✅ Shoonya Login Successful")
                            feed_opened.clear()
                            self._ws_stopped.clear()
                            ws_thread = threading.Thread(
                                target=self.api.start_websocket,
                                kwargs={"subscribe_callback": on_tick, "socket_open_callback": on_open},
                                daemon=True
                            )
                            ws_thread.start()
                            await asyncio.to_thread(feed_opened.wait, 20.0)
                        else:
                            logger.error(f"❌ Shoonya Login Failed: {ret}")
                            await asyncio.sleep(10)
                            continue

                # 2. Ingest Next Tick
                try:
                    raw_tick = await asyncio.wait_for(self.tick_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                # 3. Validation & Parsing
                if raw_tick.get('t') not in ('tk', 'tf'):
                    continue

                token = str(raw_tick.get('tk'))
                symbol = TOKEN_TO_SYMBOL.get(token) or self.active_option_tokens.get(token)
                if not symbol:
                    # logger.debug(f"Unknown token: {token}")
                    continue
                
                # logger.info(f"DEBUG: Processing tick for {symbol} (Token: {token}) ID: {hex(id(symbol))}")

                # 4. Standardize Internal Tick Format
                price = float(raw_tick.get('lp', self._prices.get(symbol, 0.0)))
                prev_price = self._prices.get(symbol, price)
                self._prices[symbol] = price
                
                curr_oi = int(raw_tick.get('oi', self._oi.get(symbol, 0)))
                prev_oi = self._oi.get(symbol, curr_oi)
                self._oi[symbol] = curr_oi

                if not getattr(self, '_data_flow_alert_sent', False):
                    from core.alerts import send_cloud_alert
                    source = "SIMULATED" if (self.sim_mode or self.off_hour_sim) else "LIVE"
                    asyncio.create_task(send_cloud_alert(f"✅ DATA INGESTION {source}: Market ticks active.", alert_type="INFO"))
                    self._data_flow_alert_sent = True

                tick = {
                    "symbol": symbol,
                    "price": price,
                    "prev_price": prev_price,
                    "volume": int(raw_tick.get('v', 1)),
                    "last_volume": int(raw_tick.get('v', 1)),
                    "oi": curr_oi,
                    "prev_oi": prev_oi,
                    "bid": float(raw_tick.get('bp1', price - 0.05)),
                    "ask": float(raw_tick.get('sp1', price + 0.05)),
                    "bid_vol": int(raw_tick.get('bq1', 100)),
                    "ask_vol": int(raw_tick.get('sq1', 100)),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "type": "TICK"
                }

                # 5. Enrichment & Storage
                # [OF-01] Pressure Gauge (OI Accel)
                oi_diff = tick["oi"] - tick["prev_oi"]
                if abs(oi_diff) > 500: # Consistent with original threshold
                    tick["oi_accel"] = float(oi_diff)
                    await self.redis_client.set(f"OI_ACCEL:{symbol}", str(oi_diff))
                else:
                    tick["oi_accel"] = 0.0
                
                # Persistence & Publication
                await self.redis_client.setnx(f"DAY_OPEN:{symbol}", str(price))

                # [D-39] Identify Option Type and Store OI for PCR calculation
                if " CE" in symbol:
                    base_asset = symbol.split()[0] # e.g. "NIFTY"
                    await self.redis_client.set(f"OI:CE:{base_asset}", str(raw_tick.get('oi', 0)))
                elif " PE" in symbol:
                    base_asset = symbol.split()[0]
                    await self.redis_client.set(f"OI:PE:{base_asset}", str(raw_tick.get('oi', 0)))

                async with self.redis_client.pipeline(transaction=True) as pipe:
                    pipe.set(f"latest_tick:{symbol}", json.dumps(tick, cls=NumpyEncoder))
                    history_key = f"tick_history:{symbol}"
                    pipe.rpush(history_key, json.dumps(tick, cls=NumpyEncoder))
                    pipe.ltrim(history_key, -2000, -1)
                    await pipe.execute()
                
                # ZMQ Publication to both Raw and TICK topics
                await self.mq.send_json(self.pub_socket, Topics.TICK_DATA, tick)
                await self.mq.send_json(self.pub_socket, f"TICK.{symbol}", tick)
                
                self._last_tick_ts[symbol] = time.time()

                # Optimized Strike Sync (Multi-Index)
                if symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"] and random.random() < 0.05:
                    ce = await self.get_optimal_strike(price, "call")
                    pe = await self.get_optimal_strike(price, "put")
                    base = "NIFTY" if symbol == "NIFTY50" else symbol
                    await self.redis_client.hset("optimal_strikes", mapping={f"{base}_CE": ce, f"{base}_PE": pe})

            except asyncio.CancelledError:
                break
            except zmq.Again:
                continue
            except Exception as e:
                logger.error(f"Tick Stream Loop Error: {e}")
                await asyncio.sleep(1)
                if not self.sim_mode and not self.off_hour_sim:
                    self._ws_stopped.set()

    # ── Dynamic Option Subscriptions ──────────────────────────────────────────

    async def _dynamic_subscription_manager(self):
        """Periodically refreshes ATM option subscriptions based on spot price."""
        logger.info("Dynamic option subscription manager active.")
        while True:
            if self._system_halted:
                await asyncio.sleep(10)
                continue
                
            try:
                # 1. Selection logic for all major indices
                for idx in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    spot = self._prices.get(idx)
                    if not spot: continue
                    
                    # Fetch Expiry (Once per day or if stale)
                    now_ts = datetime.now().timestamp()
                    if now_ts - self.last_expiry_sync > 3600: # 1h
                        await self._sync_expiries()
                        self.last_expiry_sync = now_ts
                    
                    # Select ideal strikes
                    ce_strike = await self.get_optimal_strike(spot, "call")  # [F9-01] Added missing await
                    pe_strike = await self.get_optimal_strike(spot, "put")   # [F9-01] Added missing await
                    
                    # Construct and Subscribe
                    shoonya_symbol = "NIFTY" if idx == "NIFTY50" else idx
                    await self._ensure_option_subscription(shoonya_symbol, ce_strike, "CE")
                    await self._ensure_option_subscription(shoonya_symbol, pe_strike, "PE")
                    
            except Exception as e:
                logger.error(f"Subscription manager error: {e}")
            
            await asyncio.sleep(300) # Every 5 mins

    async def _sync_expiries(self):
        """Fetches the nearest expiry for all major indices from Shoonya."""
        indices = [
            {"id": "NIFTY50", "tradingsymbol": "NIFTY", "exchange": "NFO"},
            {"id": "BANKNIFTY", "tradingsymbol": "BANKNIFTY", "exchange": "NFO"},
            {"id": "SENSEX", "tradingsymbol": "SENSEX", "exchange": "BFO"}
        ]

        if os.getenv("SIMULATION_MODE", "false").lower() == "true" or not is_market_hours():
            # Mock expiries for simulation/off-hours
            for idx in indices:
                await self.redis_client.set(f"EXPIRY:{idx['id']}", "26MAR")
            # Legacy compatibility
            await self.redis_client.set("CURRENT_EXPIRY_DATE", "26MAR")
            return
            
        for idx in indices:
            try:
                # Use a mid-strike to get the chain
                strike = 25000 if idx['tradingsymbol'] == "NIFTY" else (50000 if idx['tradingsymbol'] == "BANKNIFTY" else 75000)
                res = self.api.get_option_chain(exchange=idx['exchange'], tradingsymbol=idx['tradingsymbol'], strike=strike, count=1)
                if res and isinstance(res, dict) and res.get('stat') == 'Ok':
                    values = res.get('values', [])
                    if values:
                        expiry = values[0].get('exDate') # e.g. "26-MAR-2026"
                        parts = expiry.split('-')
                        formatted = f"{parts[0]}{parts[1]}"
                        await self.redis_client.set(f"EXPIRY:{idx['id']}", formatted)
                        
                        # Set legacy key for NIFTY to prevent breakage until all consumers are updated
                        if idx['id'] == "NIFTY50":
                            await self.redis_client.set("CURRENT_EXPIRY_DATE", formatted)
                            
                        logger.info(f"Sync'd expiry for {idx['id']}: {formatted}")
            except Exception as e:
                logger.error(f"Expiry sync failed for {idx['id']}: {e}")

    async def _ensure_option_subscription(self, idx: str, strike: float, otype: str):
        """Finds token for strike/type and subscribes if not active."""
        # Standardize NIFTY to NIFTY50 for internal Redis keys
        asset_id = "NIFTY50" if idx == "NIFTY" else idx
        
        # Pull from per-asset expiry key
        expiry = await self.redis_client.get(f"EXPIRY:{asset_id}") or "26MAR"
        exch = "BFO" if asset_id == "SENSEX" else "NFO"
        
        search_text = f"{idx} {expiry} {int(strike)} {otype}"
        
        try:
            # Mock if in simulation mode
            if os.getenv("SIMULATION_MODE", "false").lower() == "true":
                fake_token = f"OPT_{idx}_{int(strike)}_{otype}"
                self.active_option_tokens[fake_token] = f"{idx} {expiry} {int(strike)} {otype}"
                return

            res = self.api.search_scrip(exchange=exch, searchtext=search_text)
            if res and res.get('stat') == 'Ok':
                values = res.get('values', [])
                if values:
                    token = values[0].get('token')
                    tsym = values[0].get('tsym')
                    
                    if token not in self.active_option_tokens:
                        logger.info(f"New ATM Option found: {tsym} (Token: {token})")
                        self.active_option_tokens[token] = tsym
                        self.api.subscribe(f"{exch}|{token}")
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
        Fetches lot sizes from Shoonya API by searching for ATM options.
        Falls back to hardcoded defaults if API unavailable.
        """
        lot_sizes = dict(DEFAULT_LOT_SIZES)

        try:
            # Mock for simulation
            if os.getenv("SIMULATION_MODE", "false").lower() == "true":
                lot_sizes.update({"NIFTY50": 75, "BANKNIFTY": 15, "SENSEX": 10})
            else:
                # [Audit Fix] Dynamically fetch lot sizes for major indices
                for idx, sym in [("NIFTY50", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("SENSEX", "SENSEX")]:
                    exch = "BFO" if idx == "SENSEX" else "NFO"
                    # Search for any derivative to get the 'ls' (lot size) field
                    res = self.api.search_scrip(exchange=exch, searchtext=sym)
                    if res and res.get('stat') == 'Ok':
                        values = res.get('values', [])
                        # Look for an entry with an 'ls' field
                        for val in values:
                            if 'ls' in val:
                                lot_sizes[idx] = int(val['ls'])
                                logger.info(f"Dynamic Lot Size for {idx}: {lot_sizes[idx]}")
                                break
            logger.info(f"Lot sizes finalized: {lot_sizes}")
        except Exception as e:
            logger.warning(f"Lot size fetch failed ({e}). Using existing values: {lot_sizes}")

        # Store in Redis
        await self.redis_client.hset("lot_sizes", mapping=lot_sizes)
        await self.redis_client.hset("tick_sizes", mapping=TICK_SIZES)
        await self.redis_client.hset("tick_ratios", mapping=TICK_RATIOS)
        await self.redis_client.publish(
            "system_events",
            json.dumps({
                "event": "ASSET_PARAMS_UPDATED", 
                "lot_sizes": lot_sizes,
                "tick_sizes": TICK_SIZES,
                "tick_ratios": TICK_RATIOS
            })
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
                        # Wave 4.1 Fix: Strictly filter alerts to Indices only (NIFTY50, BANKNIFTY, SENSEX)
                        # And enforce a 30s global alert throttle.
                        if symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                            if (now - self._last_stale_alert_ts) > 30:
                                logger.warning(f"STALE FEED: {symbol} last tick {age_ms:.0f}ms ago. Triggering socket reset...")
                                await self._force_socket_reset(symbol, silent=False)
                                
                                if is_market_hours() and not os.getenv("SIMULATION_MODE", "false").lower() == "true":
                                    asyncio.create_task(send_cloud_alert(
                                        f"⚠️ DATA GATEWAY: Live feed for {symbol} is stale (>{age_ms:.0f}ms). Attempting reset.", 
                                        alert_type="WARNING"
                                    ))
                                self._last_stale_alert_ts = now
                        else:
                            # Silent reset for individual stocks, no logging, no alerts.
                            await self._force_socket_reset(symbol, silent=True)
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
            await asyncio.sleep(0.5)

    async def _force_socket_reset(self, symbol: str, silent: bool = False):
        """
        Forces a full WebSocket reconnection for a stale feed.
        """
        if not silent:
            logger.warning(f"🔄 Socket reset triggered for {symbol}. Forcing WebSocket restart...")
            
        # 1. Signal the main tick_stream loop to reconnect
        self._ws_reconnect_flag = True
        
        # 2. Explicitly close the current websocket (if it's hung)
        try:
            # We use to_thread because close_websocket might block
            await asyncio.to_thread(self.api.close_websocket)
            logger.info("Successfully requested Shoonya WS closure.")
        except Exception as e:
            if not silent:
                logger.error(f"Error during close_websocket: {e}")
                
        # 3. Reset the last tick timestamp to avoid repeated triggers
        self._last_tick_ts[symbol] = datetime.now(timezone.utc).timestamp()

        await self.redis_client.publish("system_events", json.dumps({
            "event": "FEED_RESET",
            "symbol": symbol,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "silent": silent
        }))

    # ── PCR Heuristic Ingestion (Phase 0) ────────────────────────────────────

    async def _pcr_ingestion_loop(self):
        """Fetches live Put-Call Ratio (PCR) for Nifty & BankNifty every 5 minutes."""
        logger.info("PCR ingestion loop active.")
        while True:
            if not is_market_hours() and not self.sim_mode:
                await asyncio.sleep(60)
                continue

            try:
                for redis_symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    shoonya_symbol = "NIFTY" if redis_symbol == "NIFTY50" else redis_symbol
                    pcr = await self._calculate_pcr(shoonya_symbol)
                    if pcr:
                        await self.redis_client.set(f"live_pcr:{redis_symbol}", pcr)
                        logger.info(f"📈 PCR {redis_symbol}: {pcr:.2f}")
            except Exception as e:
                logger.error(f"PCR ingestion error: {e}")
            
            await asyncio.sleep(300) # 5-minute interval

    async def _calculate_pcr(self, symbol: str) -> float | None:
        """Calculates PCR by summing OI of Puts / Calls for the nearest expiry."""
        if self.sim_mode:
            return random.uniform(0.7, 1.3)

        try:
            # Get current expiry token/price to find ATM
            redis_symbol = "NIFTY50" if symbol == "NIFTY" else symbol
            spot = self._prices.get(redis_symbol)
            if not spot: return None

            expiry = await self.redis_client.get(f"EXPIRY:{redis_symbol}") or "26MAR"
            exch = "BFO" if redis_symbol == "SENSEX" else "NFO"
            
            # Shoonya get_option_chain expects strike for chain discovery
            # We'll pull strikes +/- 500 around spot to get most the OI
            res = self.api.get_option_chain(exchange=exch, tradingsymbol=symbol, strike=round(spot/50)*50, count=10)
            
            if res and res.get('stat') == 'Ok':
                values = res.get('values', [])
                call_oi: int = 0
                put_oi: int = 0
                for v in values:
                    # Filter for current expiry only
                    # Scrip name: NIFTY26MAR22350CE
                    tsym = v.get('tsym', '')
                    if expiry not in tsym: continue
                    
                    oi = int(v.get('oi', 0))
                    if tsym.endswith('CE'):
                        call_oi += oi
                    elif tsym.endswith('PE'):
                        put_oi += oi
                
                if call_oi > 0:
                    pcr = put_oi / call_oi
                    
                    # [SB-01] Identify Structural Walls (Major Resistance/Support)
                    # We find the strike with max OI in the chain
                    ce_wall = max(values, key=lambda x: int(x.get('oi', 0)) if x.get('tsym', '').endswith('CE') else 0)
                    pe_wall = max(values, key=lambda x: int(x.get('oi', 0)) if x.get('tsym', '').endswith('PE') else 0)
                    
                    await self.redis_client.set(f"CALL_WALL:{redis_symbol}", ce_wall.get('tsym', '—'))
                    await self.redis_client.set(f"PUT_WALL:{redis_symbol}", pe_wall.get('tsym', '—'))
                    
                    return pcr
                    
        except Exception as e:
            logger.error(f"Failed to calculate PCR for {symbol}: {e}")
        return None

    # ── SEBI Circuit Breaker Monitor ─────────────────────────────────────────

    async def _circuit_breaker_monitor(self):
        """
        Detects SEBI-mandated circuit breaker halts (10% / 15% / 20% index moves).
        Broadcasts SYSTEM_HALT with time-of-day appropriate sleep duration.
        """
        logger.info("Circuit breaker monitor active.")
        # [F1-03] Wait for first prices to populate before monitoring
        while "NIFTY50" not in self._prices or "BANKNIFTY" not in self._prices:
            await asyncio.sleep(2)
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
        asyncio.create_task(send_cloud_alert(
            f"⛔ CIRCUIT BREAKER L{level}% triggered! Market halt: {halt_mins} min.",
            alert_type="CRITICAL"
        ))

    # ── History Sync Scheduler (Centralized) ──────────────────────────────────
    
    async def _history_sync_scheduler(self):
        """Schedules and executes the 14-day daily close fetch (Moved from SystemController)."""
        logger.info("History sync scheduler active.")
        while True:
            try:
                # 1. Boot-time check
                if not self._history_synced:
                    if not await self.redis_client.exists("history_14d:NIFTY50"):
                        logger.info("🚀 Data missing on boot. Triggering 14-day historical fetch...")
                        await self._fetch_14d_history()
                    self._history_synced = True

                # 2. Daily schedule at 09:00 IST
                now = datetime.now(tz=IST)
                if now.hour == 9 and now.minute == 0:
                    logger.info("🕒 Scheduled 09:00 IST 14-day history sync...")
                    await self._fetch_14d_history()
                    await asyncio.sleep(65) # Avoid double trigger
                
            except Exception as e:
                logger.error(f"History sync scheduler error: {e}")
            
            await asyncio.sleep(30)

    async def _fetch_14d_history(self):
        """Fetches 14 days of history for major indices (NIFTY, BANKNIFTY, SENSEX)."""
        if self.sim_mode:
            logger.info("🧪 Generating comprehensive mock 14D history for indices...")
            for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                base = 22000.0 if asset == "NIFTY50" else (47500.0 if asset == "BANKNIFTY" else 72000.0)
                mock_closes = [base + random.uniform(-200, 200) for _ in range(14)]
                await self.redis_client.set(f"history_14d:{asset}", json.dumps(mock_closes))
            self._history_synced = True
            return

        try:
            # Note: self.api is already logged in by _tick_stream
            indices = [
                {"id": "NIFTY50", "exch": "NSE", "token": "26000"},
                {"id": "BANKNIFTY", "exch": "NSE", "token": "26001"},
                {"id": "SENSEX", "exch": "BSE", "token": "1"}
            ]

            for idx in indices:
                end_time = datetime.now().timestamp()
                start_time = end_time - (20 * 86400) # 20 days buffer
                
                ret = await asyncio.to_thread(self.api.get_time_price_series,
                    exchange=idx['exch'], token=idx['token'], 
                    starttime=start_time, endtime=end_time, interval=None
                )
                
                if ret and isinstance(ret, list):
                    # 'into' or 'c' field for close
                    closes = [float(c.get('into', c.get('c', 0))) for c in ret[-14:]]
                    await self.redis_client.set(f"history_14d:{idx['id']}", json.dumps(closes))
                    logger.info(f"✅ Centralized Sync: Stored 14D history for {idx['id']}")
            
        except Exception as e:
            logger.error(f"Centralized history fetch failed: {e}")

    # ── Standalone Post-Market Simulator ──────────────────────────────────────

    async def _run_simulator(self):
        """
        Broadcasting simulator that cycles through ALL assets to fill all dashboard slots.
        Starts if SIMULATION_MODE=true or ENABLE_OFF_HOUR_SIMULATOR=true.
        """
        logger.info("🚀 High-Fidelity Simulator task initialized.")
        while True:
            # Active if hard simulation is on OR if off-hour sim is enabled
            if not self.sim_mode and not self.off_hour_sim:
                await asyncio.sleep(10)
                continue
            
            # Additional check: only run off-hour sim if real market is closed
            # (To avoid overlapping with real ticks if some are still coming)
            now = datetime.now(tz=IST)
            if self.off_hour_sim and not self.sim_mode:
                is_market_hours = (9, 15) <= (now.hour, now.minute) <= (15, 30)
                if is_market_hours:
                    await asyncio.sleep(60)
                    continue
            try:
                # [Audit-Fix] Enhanced Simulator: Generate multi-strike options for indices
                current_indices = ["NIFTY50", "BANKNIFTY", "SENSEX"]
                for asset in current_indices:
                    spot = self._prices.get(asset, 22000.0 if asset == "NIFTY50" else (47500.0 if asset == "BANKNIFTY" else 72000.0))
                    step = 50 if asset == "NIFTY50" else (100 if asset == "BANKNIFTY" else 100)
                    atm_strike = round(spot / step) * step
                    expiry = await self.redis_client.get(f"EXPIRY:{asset}") or "26MAR"
                    base_sym = "NIFTY" if asset == "NIFTY50" else asset
                    
                    # Simulate 5 OTM strikes for both sides to create walls/pivots
                    for i in range(-5, 6):
                        strike = atm_strike + (i * step)
                        for otype in ["CE", "PE"]:
                            opt_sym = f"{base_sym} {expiry} {int(strike)} {otype}"
                            token = f"SIM_{base_sym}_{int(strike)}_{otype}"
                            self.active_option_tokens[token] = opt_sym
                            
                            # Initial price heuristic (simple Intrinsic + Time value)
                            dist = abs(spot - strike)
                            intrinsic = max(0, (spot - strike) if otype == "CE" else (strike - spot))
                            opt_price = intrinsic + max(5, 50 - dist * 0.1)
                            
                            # WALL Simulation: Pick a strike for high OI
                            # Nifty Call Wall at +200, Put Wall at -200
                            is_wall = (i == 4 and otype == "CE") or (i == -4 and otype == "PE")
                            oi = random.randint(5000000, 8000000) if is_wall else random.randint(500000, 2000000)
                            
                            raw_tick = {
                                't': 'tk', 'tk': token, 'lp': str(round(opt_price, 2)), 
                                'v': str(random.randint(50, 200)), 'oi': str(oi),
                                'bp1': str(round(opt_price - 0.1, 2)), 'sp1': str(round(opt_price + 0.1, 2)),
                                'bq1': "500", 'sq1': "500"
                            }
                            await self.tick_queue.put(raw_tick)

                # Cycle through existing watched symbols
                for symbol in SYMBOLS_UNDERLYING:
                    base_price = self._prices.get(symbol)
                    if not base_price:
                        if symbol == "NIFTY50": base_price = 22000.0
                        elif symbol == "BANKNIFTY": base_price = 47500.0
                        elif symbol == "SENSEX": base_price = 72000.0
                        elif symbol == "RELIANCE": base_price = 2950.0
                        elif symbol == "HDFCBANK": base_price = 1450.0
                        else: base_price = 1000.0 + random.uniform(500, 2000)
                        self._prices[symbol] = base_price
                        
                    price = base_price * (1 + random.uniform(-0.0002, 0.0002))
                    self._prices[symbol] = price
                    
                    token = "FAKE_TOKEN"
                    for t, s in TOKEN_TO_SYMBOL.items():
                        if s == symbol: token = t; break
                    
                    raw_tick = {
                        't': 'tk', 'tk': token, 'lp': str(round(price, 2)), 
                        'v': str(random.randint(10, 500)), 'oi': str(random.randint(1000000, 5000000)),
                        'bp1': str(round(price - 0.05, 2)), 'sp1': str(round(price + 0.05, 2)),
                        'bq1': str(random.randint(1000, 5000)), 'sq1': str(random.randint(1000, 5000))
                    }
                    await self.tick_queue.put(raw_tick)
                
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"Simulator cycle error: {e}")
                await asyncio.sleep(1)

    async def _config_update_listener(self):
        """Listens for dynamic configuration changes from Redis."""
        pubsub = self.redis_client.pubsub()
        try:
            await pubsub.subscribe("config_updates")
            logger.info("Config update listener active.")
            async for message in pubsub.listen():
                if message["type"] == "message":
                    pass
        except Exception as e:
            logger.error(f"Config listener error: {e}")

async def start_gateway():
    gw = DataGateway()
    try:
        await gw.start()
    except KeyboardInterrupt:
        logger.info("DataGateway shutting down.")
    finally:
        if gw.pub_socket: gw.pub_socket.close()
        gw.mq.context.term()
        if gw.redis_client: await gw.redis_client.aclose()

if __name__ == "__main__":
    if uvloop: uvloop.install()
    elif hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(start_gateway())
