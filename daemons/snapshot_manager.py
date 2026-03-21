"""
daemons/snapshot_manager.py
===========================
Slow Lane: REST API polling for structural market data, account stats, and history.
Handles "heavy lifting" logic to avoid interfering with TickSensor's Fast Lane.
"""

import asyncio
import json
import logging
import os
import sys
import time
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pyotp
import redis.asyncio as redis
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
logger = logging.getLogger("SnapshotManager")

IST = ZoneInfo("Asia/Kolkata")

# [Parity] SEBI Circuit Breaker definitions
CIRCUIT_BREAKER_MATRIX = {
    10: {"before_1_pm": 45, "before_2_30_pm": 15, "after_2_30_pm": 0},
    15: {"before_1_pm": 105, "before_2_30_pm": 45, "after_2_30_pm": 0},
    20: {"before_1_pm": 0, "before_2_30_pm": 0, "after_2_30_pm": 0},
}

# [Parity] SHM Slot Mapping (Must match TickSensor)
SYMBOL_TO_SLOT = {
    "NIFTY50": 0, "BANKNIFTY": 1, "RELIANCE": 2, "HDFCBANK": 3, "INFY": 4,
    "TCS": 5, "ICICIBANK": 6, "SENSEX": 7, "ITC": 8, "SBIN": 9,
    "AXISBANK": 10, "KOTAKBANK": 11, "LT": 12, "INDIAVIX": 13
}


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
        
        # [Parity] Tick SHM (Attach mode)
        try:
            self.shm_ticks = TickSharedMemory(create=False)
            logger.info("✅ Attached to Tick Shared Memory (Slow Lane).")
        except:
            self.shm_ticks = None

    async def start(self):
        from core.auth import get_redis_url
        self.redis_client = redis.from_url(get_redis_url(), decode_responses=True)
        await self.redis_client.ping()
        
        logger.info("🚀 SnapshotManager (Slow Lane) active.")
        
        tasks = [
            self._price_sync_loop(),
            self._option_chain_scanner(),
            self._dynamic_subscription_manager(), # [Parity]
            self._account_sync_loop(),
            self._history_sync_scheduler(),
            self._lot_size_scheduler(),
            self._circuit_breaker_monitor(),
            self._config_update_listener(),
            self._run_simulator(),
            self._run_heartbeat()
        ]
        await asyncio.gather(*tasks)

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
            if not self.api: await self._connect_shoonya()
            for asset, search in [("NIFTY50", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("SENSEX", "SENSEX")]:
                res = await asyncio.to_thread(self.api.search_scrip, exchange="NFO", searchtext=search)
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
                if not self.api: await self._connect_shoonya()
                for idx, sym in [("NIFTY50", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"), ("SENSEX", "SENSEX")]:
                    res = await asyncio.to_thread(self.api.search_scrip, exchange="NFO", searchtext=sym)
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
        logger.info("Full Circuit Breaker monitor active.")
        # [Parity] Wait for base prices to populate
        while "NIFTY50" not in self._prices or "BANKNIFTY" not in self._prices:
            await asyncio.sleep(2)
        base_nifty = self._prices["NIFTY50"]
        base_banknifty = self._prices["BANKNIFTY"]

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
        logger.info("Option chain scanner active.")
        while True:
            try:
                if not self.api and not self.sim_mode:
                    await self._connect_shoonya()
                
                for asset in ["NIFTY50", "BANKNIFTY", "SENSEX"]:
                    spot = self._prices.get(asset)
                    if not spot: continue
                    
                    shoonya_symbol = "NIFTY" if asset == "NIFTY50" else asset
                    step = 50 if asset == "NIFTY50" else 100
                    atm = round(spot / step) * step
                    expiry = await self.redis_client.get(f"EXPIRY:{asset}") or "26MAR"
                    exch = "BFO" if asset == "SENSEX" else "NFO"

                    if self.sim_mode:
                        # Simulated Wall Logic (Already implemented in TickSensor sim if needed)
                        continue

                    # Fetch a broader chain (+/- 15 strikes)
                    res = await asyncio.to_thread(self.api.get_option_chain, 
                                                 exchange=exch, tradingsymbol=shoonya_symbol, 
                                                 strike=atm, count=15)
                    
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
                            logger.info(f"📈 {asset} PCR: {pcr:.2f}")

                        # JIT Subscription requests
                        for v in valid_strikes:
                            strike_val = float(v.get('strprc', 0))
                            if abs(strike_val - spot) < 300: # JIT Window
                                await self.redis_client.publish("tick_sensor:subscriptions", json.dumps({
                                    "action": "subscribe", "symbol": f"{exch}|{v['token']}", "tsym": v['tsym']
                                }))
            except Exception as e:
                logger.error(f"Option scanner error: {e}")
            await asyncio.sleep(30) # Poll every 30s instead of 10s for stability

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

            if not self.api: await self._connect_shoonya()
            res = await asyncio.to_thread(self.api.search_scrip, exchange=exch, searchtext=search_text)
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
                sim_ltt = (datetime.combine(now.date(), datetime.min.time()) + 
                           (now - datetime.combine(now.date(), datetime.min.time()))).strftime("%H:%M:%S")
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
                            await self.mq.send_json(self.mq.create_publisher(Ports.MARKET_DATA), Topics.TICK_DATA, tick)
                            
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
        from core.auth import HEDGE_RESERVE_PCT # Assuming it's in core or hardcode 0.15
        RES_PCT = 0.15 

        while True:
            try:
                if not self.api and not self.sim_mode: await self._connect_shoonya()
                if self.sim_mode:
                    await self.redis_client.set("ACCOUNT:MARGIN:AVAILABLE", "1000000.0")
                    # Mock regulatory components
                    await self.redis_client.set("CASH_COMPONENT_LIVE", "425000.0")
                    await self.redis_client.set("COLLATERAL_COMPONENT_LIVE", "425000.0")
                    await self.redis_client.set("HEDGE_RESERVE_LIVE", "150000.0")
                    await asyncio.sleep(30); continue

                # 1. Margin Limits
                limits = await asyncio.to_thread(self.api.get_limits)
                if limits and limits.get('stat') == 'Ok':
                    total_cash = float(limits.get('cash', 0))
                    used = float(limits.get('marginused', 0))
                    await self.redis_client.set("ACCOUNT:MARGIN:AVAILABLE", str(total_cash))
                    await self.redis_client.set("ACCOUNT:MARGIN:USED", str(used))
                    
                    # Regulatory splits for SystemController
                    eff = total_cash * (1 - RES_PCT)
                    await self.redis_client.set("CASH_COMPONENT_LIVE", f"{eff * 0.5:.2f}")
                    await self.redis_client.set("COLLATERAL_COMPONENT_LIVE", f"{eff * 0.5:.2f}")
                    await self.redis_client.set("HEDGE_RESERVE_LIVE", f"{total_cash * RES_PCT:.2f}")

                # 2. Positions (Truth Map for Ghost Sync)
                positions = await asyncio.to_thread(self.api.get_positions)
                if isinstance(positions, list):
                    truth_map = {p['tsym']: int(p.get('netqty', 0)) for p in positions if int(p.get('netqty', 0)) != 0}
                    if truth_map:
                        await self.redis_client.delete("broker_positions") # Refresh hash
                        await self.redis_client.hset("broker_positions", mapping=truth_map)
                        logger.debug(f"Synced {len(truth_map)} positions to Redis.")

            except Exception as e:
                logger.error(f"Account sync error: {e}")
            await asyncio.sleep(30)

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
                
                ret = await asyncio.to_thread(self.api.get_time_price_series,
                    exchange=idx['exch'], token=idx['token'], 
                    starttime=start_time, endtime=end_time, interval=None
                )
                
                if ret and isinstance(ret, list):
                    closes = [float(c.get('into', c.get('c', 0))) for c in ret[-14:]]
                    await self.redis_client.set(f"history_14d:{idx['id']}", json.dumps(closes))
                    logger.info(f"✅ History Sync: Stored 14D history for {idx['id']}")
            
        except Exception as e:
            logger.error(f"History fetch failed: {e}")

    async def _connect_shoonya(self):
        try:
            host = os.getenv("SHOONYA_HOST")
            self.api = NorenApi(host=host)
            totp = pyotp.TOTP(os.getenv("SHOONYA_FACTOR2")).now()
            await asyncio.to_thread(self.api.login, 
                userid=os.getenv("SHOONYA_USER"), password=os.getenv("SHOONYA_PWD"),
                twoFA=totp, vendor_code=os.getenv("SHOONYA_VC"),
                api_secret=os.getenv("SHOONYA_APP_KEY"), imei=os.getenv("SHOONYA_IMEI")
            )
        except Exception as e:
            logger.error(f"Rest login failed: {e}")

if __name__ == "__main__":
    if uvloop: uvloop.install()
    elif hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    manager = SnapshotManager()
    asyncio.run(manager.start())
