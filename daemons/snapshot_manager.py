"""
Structural Data & Snapshot Manager
Handles slow-lane REST polling for accounts, history, and option chains.
Monitors SEBI circuit breakers and runs the off-hour simulator.
"""

import asyncio
import json
import logging
import os
import sys
import time
import random
import httpx
import csv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pyotp
import redis.asyncio as redis
from dotenv import load_dotenv

try:
    import uvloop
except ImportError:
    uvloop = None

try:
    import asyncpg
    _HAS_ASYNCPG = True
except ImportError:
    _HAS_ASYNCPG = False

from core.mq import MQManager, Ports, Topics, NumpyEncoder
from core.alerts import send_cloud_alert
from core.shared_memory import TickSharedMemory, SYMBOL_TO_SLOT
from NorenRestApiPy.NorenApi import NorenApi
from core.auth import get_redis_url

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("SnapshotManager")

IST = ZoneInfo("Asia/Kolkata")

# [Parity] SEBI Circuit Breaker definitions
CIRCUIT_BREAKER_MATRIX = {
    10: {"before_1_pm": 45, "before_2_30_pm": 15, "after_2_30_pm": 0},
    15: {"before_1_pm": 105, "before_2_30_pm": 45, "after_2_30_pm": 0},
    20: {"before_1_pm": 0, "before_2_30_pm": 0, "after_2_30_pm": 0},
}

HEDGE_RESERVE_PCT = 0.15  # 15% margin reserve for regulatory safety


class SnapshotManager:
    def __init__(self):
        self.mq = MQManager()
        self.redis_client = None
        self.api = None
        self.sim_mode = os.getenv("SIMULATION_MODE", "false").lower() == "true"
        self.off_hour_sim = os.getenv("ENABLE_OFF_HOUR_SIMULATOR", "false").lower() == "true"
        self._history_synced = False
        self._prices = {} 
        self._system_halted = False
        self.last_expiry_sync = 0 # [Parity]
        self.active_option_tokens = {} # [Parity]
        self._sim_ltt_counter = 0     # [Audit-Fix] Persistent LTT for simulation
        
        # REST Gatekeeper: Enforces broker rate limits (3 requests per second)
        self.rest_semaphore = asyncio.Semaphore(1)
        self.rest_delay = 0.34
        self.pool = None # asyncpg pool for Ghost Position Sync
        self.prev_closes = {} # Ref closes for Circuit Breakers
        self.atr_values = {} # For ATR-based JIT
        
        # Cloud Clients
        self.firestore_db = None
        self.gcs_client = None
        self.gcs_bucket = os.getenv("GCS_MODEL_BUCKET", "karthiks-trading-models")
        
        # [Parity] Tick SHM (Attach mode)
        try:
            self.shm_ticks = TickSharedMemory(create=False)
            logger.info("✅ Attached to Tick Shared Memory (Slow Lane).")
        except:
            self.shm_ticks = None

    async def start(self):
        # 0. Redis Connectivity
        self.redis_client = redis.from_url(get_redis_url(), decode_responses=True)
        await self.redis_client.ping()
        
        # [Audit-Fix] Initialize Cloud Clients for Dynamic Hydration
        await self._init_cloud_clients()
        
        # [Audit-Fix] Component 3: Database Pool for Ghost Position Sync
        if _HAS_ASYNCPG:
            try:
                from core.auth import get_db_dsn
                self.pool = await asyncpg.create_pool(get_db_dsn(), min_size=1, max_size=5)
                logger.info("✅ SnapshotManager connected to TimescaleDB.")
            except Exception as e:
                logger.error(f"DB connect failed: {e}")

        logger.info("🚀 SnapshotManager (Slow Lane) active. Beginning Boot-Time Hydration...")
        
        # --- STEP 1: HYDRATION (Blocking) ---
        logger.info(f"Checking simulation mode: {self.sim_mode}")
        if not self.sim_mode:
            await self._connect_shoonya()
        await self._boot_hydration()
        
        logger.info("🌊 Hydration Complete. Launching verifier loops.")
        tasks = [
            self._price_sync_loop(),
            self._option_chain_scanner(),       # Freq: 60s
            self._dynamic_subscription_manager(),# JIT ATR-based
            self._account_sync_loop(),          # Freq: 30s -> 60s Refinement
            self._history_sync_scheduler(),
            self._lot_size_scheduler(),
            self._circuit_breaker_monitor(),    # Uses prev_close
            self._ghost_position_sync(),        # [New] Safety audit
            self._config_update_listener(),
            self._persistent_alpha_snapshots(), # [Layer 7]
            self._run_simulator(),
            self._run_heartbeat()
        ]
        await asyncio.gather(*tasks)

    async def _connect_shoonya(self):
        """[Audit-Fix] Component 1: Centralized Shoonya API connection and login."""
        if self.api: return # Already connected

        self.user = os.getenv("SHOONYA_USER")
        self.pwd = os.getenv("SHOONYA_PWD")
        self.factor2 = os.getenv("SHOONYA_FACTOR2")
        self.vc = os.getenv("SHOONYA_VC")
        self.app_key = os.getenv("SHOONYA_APP_KEY")
        self.imei = os.getenv("SHOONYA_IMEI")

        if not all([self.user, self.pwd, self.factor2, self.vc, self.app_key, self.imei]):
            logger.critical("❌ Shoonya API credentials not fully set. Check .env file.")
            raise ValueError("Shoonya API credentials missing.")

        host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP")
        ws_url = host.replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP')
        if not ws_url.endswith('/'): ws_url += '/'
        self.api = NorenApi(host=host, websocket=ws_url)

        # 2. Shoonya API Auth (Gatekeeper)
        if not self.sim_mode:
            logger.info(f"Authenticating with Shoonya API as {self.user}...")
            totp = pyotp.TOTP(self.factor2).now()
            res = self.api.login(userid=self.user, password=self.pwd, twoFA=totp, 
                           vendor_code=self.vc, api_secret=self.app_key, imei=self.imei)
            
            if res and res.get('stat') == 'Ok':
                logger.info(f"Successfully logged in to Shoonya API. Name: {res.get('uname')}")
                self._master_fetched = True # Allow history sync
            else:
                logger.error(f"❌ Shoonya API Login Failed: {res}")
                # [Audit-Fix] Critical failure alert
                asyncio.create_task(send_cloud_alert(f"CRITICAL: SnapshotManager failed Shoonya login: {res}", alert_type="ERROR"))
                raise ConnectionError(f"Shoonya API Login Failed: {res}")

    async def _call_api(self, func_name, *args, **kwargs):
        """[Audit-Fix] Component 2: Centralized REST Gatekeeper (Semaphore + Delay)."""
        if not self.api:
            await self._connect_shoonya()
            
        async with self.rest_semaphore:
            func = getattr(self.api, func_name)
            res = await asyncio.to_thread(func, *args, **kwargs)
            await asyncio.sleep(self.rest_delay) # Enforce 3 req/sec
            return res

    async def _boot_hydration(self):
        """[Audit-Fix] Phase I: Blocking Hydration (Ensures Layers 2/3/4 never see 'None')."""
        try:
            # 1. Connect first
            if not self.sim_mode:
                await self._connect_shoonya()
            
            # 2. Dynamic Constituents (New)
            await self._hydrate_dynamic_constituents()
            
            # 3. Expiries
            await self._sync_expiries()
            
            # 3. Lot/Tick Sizes
            await self._fetch_and_store_lot_sizes()
            
            # 4. History + Prev Day Close + ATR
            await self._fetch_14d_history()
            
            # 5. Initial Account State
            await self._account_sync_task()
            
        except Exception as e:
            logger.critical(f"🛑 HYDRATION FAILED: {e}")
            # Keep trying or exit? For Fortress, we retry once then proceed with warnings
            await asyncio.sleep(5)

    async def _init_cloud_clients(self):
        """Lazily initialize Google Cloud clients for dynamic hydration fallbacks."""
        try:
            from google.cloud import firestore, storage
            
            creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
            if creds_path:
                if os.path.isdir(creds_path) or (os.path.isfile(creds_path) and os.path.getsize(creds_path) == 0):
                    logger.error(f"❌ GCP Credentials Error: {creds_path} is invalid! Unsetting.")
                    os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
            
            self.firestore_db = firestore.AsyncClient()
            self.gcs_client = storage.Client()
            logger.info("✅ SnapshotManager Cloud Clients initialized.")
        except Exception as e:
            logger.warning(f"⚠️ Cloud clients disabled: {e}. Running in local-only mode.")
            self.firestore_db = None
            self.gcs_client = None

    async def _hydrate_dynamic_constituents(self):
        """[Audit-Fix] Advisory 3: Dynamic Index Constituents Hydration."""
        logger.info("📐 Hydrating Dynamic Index Constituents (The Fortress)...")
        
        indices = {
            "NIFTY50": {
                "search": "Nifty 50", 
                "csv_url": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
                "fallback": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT"]
            },
            "BANKNIFTY": {
                "search": "Nifty Bank",
                "csv_url": "https://www.niftyindices.com/IndexConstituent/ind_niftybanklist.csv",
                "fallback": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "AUBL", "FEDERALBNK", "IDFCFIRSTB", "BANDHANBNK"]
            },
            "SENSEX": {
                "search": "S&P BSE SENSEX",
                "csv_url": "https://www.bseindia.com/markets/Equity/EquityClick.aspx?reportName=EquityMarketReport", # Fallback to manual link info
                "fallback": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "TCS", "LT", "AXISBANK", "SBIN", "KOTAKBANK"]
            }
        }

        for idx, cfg in indices.items():
            constituents = []
            
            # Phase 1: Shoonya API
            try:
                res = await self._call_api('get_index_constituents', index=cfg['search'])
                if res and 'values' in res:
                    constituents = [item['tsym'] for item in res['values'][:10]]
                    logger.info(f"✅ {idx}: Fetched from Shoonya API.")
            except Exception as e:
                logger.debug(f"Shoonya API constituent fetch failed for {idx}: {e}")

            # Phase 2: Official CSV (Direct Source)
            if not constituents:
                try:
                    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'}
                    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                        resp = await client.get(cfg['csv_url'], timeout=10.0)
                        if resp.status_code == 200:
                            text = resp.text
                            lines = text.splitlines()
                            
                            reader = csv.DictReader(lines)
                            # Normalize headers to find 'Symbol' or 'SCRIP_CODE' (for Sensex)
                            constituents = []
                            for row in reader:
                                symbol = row.get('Symbol') or row.get('SCRIP_CODE') or row.get('SYMBOL')
                                if symbol: constituents.append(symbol.strip())
                                if len(constituents) >= 10: break
                                
                            if not constituents:
                                # Last resort: raw parse
                                constituents = [line.split(',')[2].strip('"').strip() for line in lines[1:11] if len(line.split(',')) > 2]
                            
                            if constituents:
                                logger.info(f"✅ {idx}: Fetched from Official CSV.")
                except Exception as e:
                    logger.debug(f"CSV fetch failed for {idx}: {e}")

            # Phase 3: Firestore Fallback
            if not constituents and self.firestore_db:
                try:
                    doc = await self.firestore_db.collection("system").document("index_constituents").get()
                    if doc.exists:
                        data = doc.to_dict()
                        constituents = data.get(idx)
                        if constituents:
                            logger.info(f"✅ {idx}: Recovered from Firestore.")
                except Exception as e:
                    logger.debug(f"Firestore recovery failed for {idx}: {e}")

            # Phase 4: GCS Fallback
            if not constituents and self.gcs_client:
                try:
                    bucket = self.gcs_client.bucket(self.gcs_bucket)
                    blob = bucket.blob("config/index_constituents.json")
                    content = await asyncio.to_thread(blob.download_as_text)
                    data = json.loads(content)
                    constituents = data.get(idx)
                    if constituents:
                        logger.info(f"✅ {idx}: Recovered from GCS Backup.")
                except Exception as e:
                    logger.debug(f"GCS recovery failed for {idx}: {e}")

            # Phase 5: Redis Fallback
            if not constituents:
                try:
                    cached = await self.redis_client.get(f"CONFIG:COMPONENTS:{idx}")
                    if cached:
                        constituents = json.loads(cached)
                        logger.info(f"✅ {idx}: Using Redis Cache.")
                except Exception as e:
                    logger.debug(f"Redis cache fallback failed for {idx}: {e}")

            # Phase 6: Hardcoded Fallback
            if not constituents:
                constituents = cfg['fallback']
                logger.warning(f"⚠️ {idx}: All sources failed. Using hardcoded defaults.")

            # --- PERSISTENCE ---
            await self.redis_client.set(f"CONFIG:COMPONENTS:{idx}", json.dumps(constituents))
            
            if self.firestore_db:
                try:
                    await self.firestore_db.collection("system").document("index_constituents").set({idx: constituents}, merge=True)
                except Exception as e:
                    logger.error(f"Failed to persist {idx} to Firestore: {e}")
            
            # Broadcasting update
            await self.redis_client.publish("system_events", json.dumps({
                "event": "DYNAMIC_COMPONENTS_UPDATED",
                "index": idx,
                "components": constituents,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }))

    async def _run_heartbeat(self):
        from core.health import HeartbeatProvider
        hb = HeartbeatProvider("SnapshotManager", self.redis_client)
        await hb.run_heartbeat()

    async def _price_sync_loop(self):
        """Syncs latest prices from Redis to locally calculate ATM strikes."""
        while True:
            try:
                for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    val = await self.redis_client.get(f"latest_tick:{asset}")
                    if val:
                        tick = json.loads(val)
                        self._prices[asset] = float(tick.get("price", 0))
            except Exception as e:
                logger.error(f"Price sync error: {e}")
            await asyncio.sleep(1)

    # ── Lot Size Scheduler ───────────────────────────────────────────────────

    async def _lot_size_scheduler(self):
        """Fetches dynamic lot sizes from broker at 09:01 IST daily."""
        logger.info("Lot size scheduler active.")
        while True:
            now = datetime.now(tz=IST)
            target = now.replace(hour=9, minute=1, second=0, microsecond=0)
            if now >= target:
                target = target.replace(day=target.day + 1)

            wait_secs = (target - now).total_seconds()
            logger.info(f"Lot size fetch scheduled in {wait_secs:.0f}s.")
            await asyncio.sleep(min(wait_secs, 60))

            now2 = datetime.now(tz=IST)
            if now2.hour == 9 and now2.minute == 1:
                await self._fetch_and_store_lot_sizes()
                await asyncio.sleep(65)

    # ── Structural Data ──────────────────────────────────────────────────────

    async def _sync_expiries(self):
        """Fetches and stores the current weekly expiry for all assets."""
        logger.info("Syncing expiries for major indices...")
        if self.sim_mode:
            for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                await self.redis_client.set(f"EXPIRY:{asset}", "26MAR26")
            return

        try:
            for asset, search in [("NIFTY50", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("SENSEX", "SENSEX")]:
                res = await self._call_api('searchscrip', exchange="NFO", searchtext=search)
                if res and res.get('stat') == 'Ok':
                    # Extract earliest expiry from search results
                    values = res.get('values', [])
                    expiries = sorted(list(set(v.get('weekly') for v in values if v.get('weekly'))))
                    if expiries:
                        await self.redis_client.set(f"EXPIRY:{asset}", expiries[0])
                        logger.info(f"📅 Expiry for {asset}: {expiries[0]}")
        except Exception as e:
            logger.error(f"Expiry sync failed: {e}")

    def get_optimal_strike(self, spot: float, step: int = 50) -> int:
        """Returns the nearest rounded strike."""
        return round(spot / step) * step

    async def _fetch_and_store_lot_sizes(self):
        lot_sizes = {"NIFTY50": 75, "BANKNIFTY": 15, "SENSEX": 10} # 2026 Sample Defaults
        try:
            if not self.sim_mode:
                for idx, sym in [("NIFTY50", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("SENSEX", "SENSEX")]:
                    res = await self._call_api('searchscrip', exchange="NFO", searchtext=sym)
                    if res and res.get('stat') == 'Ok':
                        for val in res.get('values', []):
                            if 'ls' in val: lot_sizes[idx] = int(val['ls']); break
            await self.redis_client.hset("lot_sizes", mapping=lot_sizes)
            await self.redis_client.hset("tick_sizes", mapping={"NIFTY50": 0.05, "BANKNIFTY": 0.05, "SENSEX": 1.00})
            
            # [Parity] Publish Asset Params Update
            await self.redis_client.publish("system_events", json.dumps({
                "event": "ASSET_PARAMS_UPDATED", 
                "lot_sizes": lot_sizes,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }))
            logger.info(f"Lot sizes updated and broadcasted: {lot_sizes}")
        except Exception as e:
            logger.warning(f"Lot size fetch failed: {e}")

    async def _circuit_breaker_monitor(self):
        """[Parity] Full SEBI-mandated circuit breaker logic with time-indexed halts."""
        logger.info("Full Circuit Breaker monitor active (Fortress Mode).")
        # [Audit-Fix] Component 3: Use prev_day_close from hydration
        while not all(idx in self.prev_closes for idx in ["NIFTY50", "BANKNIFTY"]):
            await asyncio.sleep(2)
        
        base_nifty = self.prev_closes["NIFTY50"]
        base_banknifty = self.prev_closes["BANKNIFTY"]

        while True:
            try:
                nifty_chg = abs(self._prices["NIFTY50"] - base_nifty) / base_nifty * 100
                bn_chg = abs(self._prices["BANKNIFTY"] - base_banknifty) / base_banknifty * 100
                max_chg = max(nifty_chg, bn_chg)
                
                halt_level = next((lv for lv in [20, 15, 10] if max_chg >= lv), None)

                if halt_level and not self._system_halted:
                    halt_mins = self._get_halt_duration(halt_level)
                    logger.critical(f"🛑 CIRCUIT BREAKER L{halt_level}%: Market moved {max_chg:.2f}%")
                    
                    await self._broadcast_halt(halt_level, halt_mins)
                    
                    if halt_mins > 0:
                        self._system_halted = True
                        await asyncio.sleep(halt_mins * 60)
                        self._system_halted = False
                        base_nifty = self._prices["NIFTY50"]
                        base_banknifty = self._prices["BANKNIFTY"]
                        await self.redis_client.set("SYSTEM_HALT", "False")
                    else:
                        # Day Halt
                        await self.redis_client.set("SYSTEM_HALT", "DAY_HALT")
                        return 
            except Exception as e:
                logger.error(f"CB monitor error: {e}")
            await asyncio.sleep(5)

    async def _broadcast_halt(self, level: int, halt_mins: int):
        """[Parity] Publishes SYSTEM_HALT event."""
        payload = {"event": "SYSTEM_HALT", "level": level, "halt_minutes": halt_mins, "timestamp": datetime.now(timezone.utc).isoformat()}
        await self.redis_client.set("SYSTEM_HALT", str(level))
        await self.redis_client.publish("system_events", json.dumps(payload))
        asyncio.create_task(send_cloud_alert(f"⛔ CIRCUIT BREAKER L{level}% triggered! Halt: {halt_mins} min.", alert_type="CRITICAL"))

    def _get_halt_duration(self, level: int) -> int:
        from datetime import time as dt_time # [Parity]
        now = datetime.now(tz=IST).time()
        matrix = CIRCUIT_BREAKER_MATRIX.get(level, {})
        if now < dt_time(13, 0): return matrix.get("before_1_pm", 0)
        elif now < dt_time(14, 30): return matrix.get("before_2_30_pm", 0)
        return matrix.get("after_2_30_pm", 0)

    async def _config_update_listener(self):
        """Listens for dynamic config changes (e.g. Risk Free Rate)."""
        pubsub = self.redis_client.pubsub()
        await pubsub.subscribe("config_updates")
        async for message in pubsub.listen():
            if message["type"] == "message":
                logger.info(f"⚙️ Config Update Received: {message['data']}")

    async def _option_chain_scanner(self):
        """Polls full option chain to identify Walls and request JIT subscriptions."""
        logger.info("Option chain scanner active (Fortress Mode - 60s).")
        while True:
            try:
                for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    spot = self._prices.get(asset)
                    if not spot: continue
                    
                    shoonya_symbol = "NIFTY" if asset == "NIFTY50" else asset
                    step = 50 if asset == "NIFTY50" else 100
                    atm = round(spot / step) * step
                    expiry = await self.redis_client.get(f"EXPIRY:{asset}") or "26MAR"
                    exch = "BFO" if asset == "SENSEX" else "NFO"

                    if self.sim_mode:
                        continue

                    # [Audit-Fix] Component 2: Gatekeeper API call
                    res = await self._call_api('get_option_chain', 
                                              exchange=exch, tradingsymbol=shoonya_symbol, 
                                              strikeprice=atm, count=15)
                    
                    if res and res.get('stat') == 'Ok':
                        values = res.get('values', [])
                        valid_strikes = [v for v in values if expiry in v.get('tsym', '')]
                        
                        if not valid_strikes: continue

                        # Identify Walls
                        ce_wall = max([v for v in valid_strikes if v.get('tsym', '').endswith('CE')], 
                                     key=lambda x: int(x.get('oi', 0)), default=None)
                        pe_wall = max([v for v in valid_strikes if v.get('tsym', '').endswith('PE')], 
                                     key=lambda x: int(x.get('oi', 0)), default=None)

                        if ce_wall: await self.redis_client.set(f"CALL_WALL:{asset}", ce_wall['tsym'])
                        if pe_wall: await self.redis_client.set(f"PUT_WALL:{asset}", pe_wall['tsym'])

                        # PCR Calculation
                        pcr = await self._calculate_pcr(shoonya_symbol, valid_strikes, expiry)
                        if pcr:
                            await self.redis_client.set(f"live_pcr:{asset}", str(round(pcr, 2)))

                        # [Audit-Fix] Component 4: ATR-based JIT Subscription Window
                        atr = self.atr_values.get(asset, 300.0)
                        jit_limit = 1.5 * atr
                        
                        for v in valid_strikes:
                            strike_val = float(v.get('strprc', 0))
                            if abs(strike_val - spot) < jit_limit: 
                                await self.redis_client.publish("tick_sensor:subscriptions", json.dumps({
                                    "action": "subscribe", "symbol": f"{exch}|{v['token']}", "tsym": v['tsym']
                                }))
            except Exception as e:
                logger.error(f"Option scanner error: {e}")
            await asyncio.sleep(60) # Reduced frequency for REST stability

    async def _dynamic_subscription_manager(self):
        """[Parity] Periodically refreshes ATM option subscriptions based on spot price."""
        logger.info("Dynamic option subscription manager active.")
        while True:
            try:
                for idx in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    spot = self._prices.get(idx)
                    if not spot: continue
                    
                    now_ts = time.time()
                    if now_ts - self.last_expiry_sync > 3600:
                        await self._sync_expiries()
                        self.last_expiry_sync = now_ts
                    
                    shoonya_symbol = "NIFTY" if idx == "NIFTY50" else idx
                    step = 50 if idx == "NIFTY50" else 100
                    atm = round(spot / step) * step
                    for otype in ["CE", "PE"]:
                        await self._ensure_option_subscription(shoonya_symbol, atm, otype)
            except Exception as e:
                logger.error(f"Sub manager error: {e}")
            await asyncio.sleep(300)

    async def _ensure_option_subscription(self, idx: str, strike: float, otype: str):
        """[Parity] Finds token for strike/type and requests subscription via Redis."""
        asset_id = "NIFTY50" if idx == "NIFTY" else idx
        expiry = await self.redis_client.get(f"EXPIRY:{asset_id}") or "26MAR"
        exch = "BFO" if asset_id == "SENSEX" else "NFO"
        search_text = f"{idx} {expiry} {int(strike)} {otype}"
        
        try:
            if self.sim_mode:
                fake_token = f"SIM_{idx}_{int(strike)}_{otype}"
                self.active_option_tokens[fake_token] = search_text
                return

            res = await self._call_api('searchscrip', exchange=exch, searchtext=search_text)
            if res and res.get('stat') == 'Ok':
                val = res.get('values', [{}])[0]
                token = val.get('token')
                if token:
                    await self.redis_client.publish("tick_sensor:subscriptions", json.dumps({
                        "action": "subscribe", "symbol": f"{exch}|{token}", "tsym": val.get('tsym')
                    }))
        except Exception as e:
            logger.error(f"Option search failed: {e}")

    async def _run_simulator(self):
        """[Parity] High-Fidelity Off-Hour Simulator (Brownian Motion + LTT Integrity)."""
        if not self.off_hour_sim and not self.sim_mode: return
        logger.info("🚀 High-Fidelity Simulator task active (Brownian Mode).")
        
        # Simulation Parameters
        DRIFT = 0.00001
        VOL = 0.0002
        
        while True:
            try:
                # Increment simulation clock
                self._sim_ltt_counter += 1
                now = datetime.now(tz=IST)
                sim_ltt = (datetime.combine(now.date(), datetime.min.time(), tzinfo=IST) + 
                           (now - datetime.combine(now.date(), datetime.min.time(), tzinfo=IST))).strftime("%H:%M:%S")
                sim_ft = time.time()

                for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    base_price = self._prices.get(asset, 22000.0 if asset == "NIFTY50" else 48000.0)
                    # Brownian Motion: dP = P * (drift * dt + vol * sqrt(dt) * epsilon)
                    change = base_price * (DRIFT + VOL * random.gauss(0, 1))
                    spot = base_price + change
                    self._prices[asset] = spot
                    
                    step = 50 if asset == "NIFTY50" else 100
                    atm = round(spot / step) * step
                    expiry = await self.redis_client.get(f"EXPIRY:{asset}") or "26MAR"
                    base_sym = "NIFTY" if asset == "NIFTY50" else asset
                    
                    for i in range(-5, 6):
                        strike = atm + (i * step)
                        for otype in ["CE", "PE"]:
                            opt_sym = f"{base_sym} {expiry} {int(strike)} {otype}"
                            intrinsic = max(0, (spot - strike) if otype == "CE" else (strike - spot))
                            # Add time value decay proxy
                            opt_price = intrinsic + max(5, 50 - abs(spot-strike) * 0.1) + random.uniform(-0.5, 0.5)
                            
                            is_wall = (i == 4 and otype == "CE") or (i == -4 and otype == "PE")
                            oi = random.randint(5000000, 8000000) if is_wall else random.randint(500000, 2000000)
                            
                            tick = {
                                "symbol": opt_sym, "price": round(opt_price, 2), "volume": random.randint(50, 200),
                                "oi": oi, "timestamp": datetime.now(timezone.utc).isoformat(), 
                                "ltt": sim_ltt, "ft": sim_ft, "source": "SIM", "type": "TICK"
                            }
                            await self.redis_client.set(f"latest_tick:{opt_sym}", json.dumps(tick, cls=NumpyEncoder))
                            # [Audit-Fix] Finding 6.4: Do not flood primary MARKET_DATA port with sim ticks
                            # await self.mq.send_json(self.mq.create_publisher(Ports.MARKET_DATA), Topics.TICK_DATA, tick)
                            
                            # Underlying update to SHM
                            if self.shm_ticks and asset in SYMBOL_TO_SLOT and i == 0 and otype == "CE":
                                self.shm_ticks.write_tick(SYMBOL_TO_SLOT[asset], asset, spot, 1000, sim_ft)
                
                # Simulate Heavyweight Stocks
                for symbol in SYMBOL_TO_SLOT.keys():
                    if symbol in ["NIFTY50", "BANKNIFTY", "SENSEX"]: continue
                    base = self._prices.get(symbol, 1500.0)
                    price = base * (1 + random.gauss(DRIFT, VOL))
                    self._prices[symbol] = price
                    
                await asyncio.sleep(1.0)
            except Exception as e:
                logger.error(f"Sim cycle error: {e}")
                await asyncio.sleep(1)

    async def _calculate_pcr(self, symbol: str, chain: list, expiry: str) -> float | None:
        """Calculates PCR by summing OI of Puts / Calls for the current expiry."""
        try:
            call_oi = sum(int(v.get('oi', 0)) for v in chain if v.get('tsym', '').endswith('CE'))
            put_oi = sum(int(v.get('oi', 0)) for v in chain if v.get('tsym', '').endswith('PE'))
            if call_oi > 0:
                return put_oi / call_oi
        except: pass
        return None

    async def _account_sync_loop(self):
        """Polls margin and positions, calculating regulatory components for SystemController."""
        while True:
            try:
                await self._account_sync_task()
            except Exception as e:
                logger.error(f"Account sync error: {e}")
            await asyncio.sleep(60) # Reduced frequency for stability

    async def _account_sync_task(self):
        """[Audit-Fix] Single pass account sync, used by hydration and loop."""

        if self.sim_mode:
            await self.redis_client.set("ACCOUNT:MARGIN:AVAILABLE", "1000000.0")
            await self.redis_client.set("CASH_COMPONENT_LIVE", "425000.0")
            await self.redis_client.set("COLLATERAL_COMPONENT_LIVE", "425000.0")
            await self.redis_client.set("HEDGE_RESERVE_LIVE", "150000.0")
            return

        # 1. Margin Limits
        limits = await self._call_api('get_limits')
        if limits and limits.get('stat') == 'Ok':
            total_cash = float(limits.get('cash', 0))
            used = float(limits.get('marginused', 0))
            await self.redis_client.set("ACCOUNT:MARGIN:AVAILABLE", str(total_cash))
            await self.redis_client.set("ACCOUNT:MARGIN:USED", str(used))
            
            # Regulatory splits for SystemController
            eff = total_cash * (1 - HEDGE_RESERVE_PCT)
            await self.redis_client.set("CASH_COMPONENT_LIVE", f"{eff * 0.5:.2f}")
            await self.redis_client.set("COLLATERAL_COMPONENT_LIVE", f"{eff * 0.5:.2f}")
            await self.redis_client.set("HEDGE_RESERVE_LIVE", f"{total_cash * HEDGE_RESERVE_PCT:.2f}")

        # 2. Positions (Truth Map for Ghost Sync)
        positions = await self._call_api('get_positions')
        if isinstance(positions, list):
            truth_map = {p['tsym']: int(p.get('netqty', 0)) for p in positions if int(p.get('netqty', 0)) != 0}
            if truth_map:
                # [Audit-Fix] Redis Atomic Overwrite: Use RENAME to prevent 'GHOST_DETECTED' race condition
                await self.redis_client.hset("broker_positions_tmp", mapping=truth_map)
                await self.redis_client.rename("broker_positions_tmp", "broker_positions")
                logger.debug(f"Synced {len(truth_map)} positions to Redis (Atomic).")
            else:
                # If no positions, clear the map safely
                await self.redis_client.delete("broker_positions")

    async def _ghost_position_sync(self):
        """[Audit-Fix] Component 3: Professional Safety Audit (Broker vs DB)."""
        if self.sim_mode or not self.pool: return
        logger.info("Ghost Position Sync active (60s frequency).")
        
        while True:
            try:
                # 1. Get broker truth from Redis (just updated by account sync)
                truth_map_raw = await self.redis_client.hgetall("broker_positions")
                truth_map = {k: int(v) for k, v in truth_map_raw.items()}
                
                # 2. Get DB truth
                async with self.pool.acquire() as conn:
                    db_positions = await conn.fetch("SELECT symbol, SUM(quantity) as qty FROM portfolio GROUP BY symbol")
                    db_map = {p['symbol']: int(p['qty']) for p in db_positions if int(p['qty']) != 0}
                
                # 3. Compare: Broker has it, DB doesn't?
                for sym, t_qty in truth_map.items():
                    d_qty = db_map.get(sym, 0)
                    if t_qty != 0 and d_qty == 0:
                        logger.critical(f"👻 GHOST ALERT: Broker sees {t_qty} of {sym} but DB is FLAT.")
                        await self.redis_client.publish("system_events", json.dumps({
                            "event": "GHOST_DETECTED",
                            "symbol": sym,
                            "broker_qty": t_qty,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        }))
            except Exception as e:
                logger.error(f"Ghost sync error: {e}")
            await asyncio.sleep(60)

    async def _history_sync_scheduler(self):
        """Hydrates 14-day history for indices."""
        while True:
            try:
                now = datetime.now(tz=IST)
                # Boot sync or Daily at 09:00
                if not self._history_synced or (now.hour == 9 and now.minute == 0):
                    await self._fetch_14d_history()
                    self._history_synced = True
                    await asyncio.sleep(65)
            except Exception as e:
                logger.error(f"History sync error: {e}")
            await asyncio.sleep(30)

    async def _fetch_14d_history(self):
        """Fetches 14 days of history for major indices (NIFTY, BANKNIFTY, SENSEX)."""
        if self.sim_mode:
            logger.info("🧪 Generating comprehensive mock 14D history for indices...")
            for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                base = 22000.0 if asset == "NIFTY50" else (47500.0 if asset == "BANKNIFTY" else 72000.0)
                mock_closes = [base + random.uniform(-200, 200) for _ in range(14)]
                await self.redis_client.set(f"history_14d:{asset}", json.dumps(mock_closes))
                
                # [Audit-Fix] Simulation Deadlock: Populate local state to bypass CB/JIT blocking loops
                self.prev_closes[asset] = mock_closes[-1]
                self.atr_values[asset] = mock_closes[-1] * 0.01 # Mock ATR: 1% of spot
            self._history_synced = True
            return

        try:
            indices = [
                {"id": "NIFTY50", "exch": "NSE", "token": "26000"},
                {"id": "BANKNIFTY", "exch": "NSE", "token": "26001"},
                {"id": "SENSEX", "exch": "BSE", "token": "1"}
            ]

            for idx in indices:
                end_time = datetime.now().timestamp()
                start_time = end_time - (20 * 86400) # 20 days buffer
                
                ret = await self._call_api('get_time_price_series',
                    exchange=idx['exch'], token=idx['token'], 
                    starttime=start_time, endtime=end_time, interval=None
                )
                
                if ret and isinstance(ret, list):
                    closes = [float(c.get('intc', c.get('c', 0))) for c in ret]
                    if closes:
                        # [Audit-Fix] Store prev_close for CB baseline
                        self.prev_closes[idx['id']] = closes[-1]
                        
                        # [Audit-Fix] Calculate EMA-based 14-day ATR (Wilder's Smoothing)
                        # ATR_t = (ATR_t-1 * 13 + TR_t) / 14
                        highs = [float(c.get('inth', c.get('h', 0))) for c in ret]
                        lows = [float(c.get('intl', c.get('l', 0))) for c in ret]
                        
                        tr = [max(h-l, abs(h-prev_c), abs(l-prev_c)) 
                              for h, l, prev_c in zip(highs[1:], lows[1:], closes[:-1])]
                        
                        if len(tr) >= 14:
                            # Seed with SMA of first 14 TRs
                            current_atr = sum(tr[:14]) / 14
                            # Smooth for remaining TRs
                            for i in range(14, len(tr)):
                                current_atr = (current_atr * 13 + tr[i]) / 14
                            atr = current_atr
                        elif tr:
                            # Standard SMA fallback if data is short
                            atr = sum(tr) / len(tr)
                        else:
                            atr = closes[-1] * 0.01 # fallback 1%
                            
                        self.atr_values[idx['id']] = atr
                        await self.redis_client.set(f"ATR:{idx['id']}", str(round(atr, 2)))

                    await self.redis_client.set(f"history_14d:{idx['id']}", json.dumps(closes, cls=NumpyEncoder))
                    logger.info(f"✅ History Sync: Stored 14D history/ATR for {idx['id']}")
            
        except Exception as e:
            logger.error(f"History fetch failed: {e}")


    async def _persistent_alpha_snapshots(self):
        """[Layer 7] Pulse task: Persists global signal state to TimescaleDB every 10s."""
        logger.info("Alpha Snapshotter active. Monitoring signal pulse...")
        while True:
            try:
                await asyncio.sleep(10.0)
                if not self.pool: continue

                # Fetch global snapshot from Redis
                # HeuristicEngine writes to 'MARKET_SIGNAL_SNAPSHOT'
                raw = await self.redis_client.get("MARKET_SIGNAL_SNAPSHOT")
                if not raw: continue
                
                snapshot = json.loads(raw)
                timestamp = datetime.now(timezone.utc)
                
                async with self.pool.acquire() as conn:
                    # Prepare batch for mass insert
                    batch = []
                    for symbol, data in snapshot.items():
                        batch.append((
                            timestamp,
                            symbol,
                            float(data.get('price', 0.0)),
                            float(data.get('s_total', 0.0)),
                            float(data.get('asto', 0.0)),
                            int(data.get('regime_s18', 0)),
                            float(data.get('quality_s27', 0.0))
                        ))
                    
                    if batch:
                        await conn.executemany(
                            """
                            INSERT INTO market_snapshots (time, symbol, price, s_total, asto, regime_s18, quality_s27)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)
                            ON CONFLICT (time, symbol) DO NOTHING
                            """,
                            batch
                        )
                # logger.debug(f"📸 Alpha Snapshot saved for {len(batch)} assets.")
            except Exception as e:
                logger.error(f"Alpha Snapshot error: {e}")
                await asyncio.sleep(5.0)

if __name__ == "__main__":
    if uvloop: uvloop.install()
    elif hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    manager = SnapshotManager()
    asyncio.run(manager.start())
