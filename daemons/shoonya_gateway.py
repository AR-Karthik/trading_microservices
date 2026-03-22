import asyncio
import json
import logging
import os
import hashlib
import time
import pyotp
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import redis.asyncio as redis
from dotenv import load_dotenv

from core.mq import MQManager, Ports
from core.mq.redis_logger import RedisLogger
from core.health import HeartbeatProvider
from core.alerts import send_cloud_alert
from NorenRestApiPy.NorenApi import NorenApi
from core.shared_memory import TickSharedMemory, MAX_SLOTS, SYMBOL_TO_SLOT
from core.auth import get_redis_url

try:
    import uvloop
except ImportError:
    uvloop = None

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.shoonya_master import get_token, get_symbol

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ShoonyaGateway")

class TokenBucketRateLimiter:
    def __init__(self, rate=1, per=1.0):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.monotonic()
    
    async def acquire(self):
        while self.tokens < 1:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.per))
            self.last_refill = now
        self.tokens -= 1

class ShoonyaDataStreamer:
    def __init__(self, redis_client, pub_socket):
        self.redis = redis_client
        self.pub_socket = pub_socket
        self.mq_manager = MQManager()
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None
        
        load_dotenv()
        self.host = os.getenv("SHOONYA_HOST", "https://api.shoonya.com/NorenWClientTP")
        self.user = os.getenv("SHOONYA_USER", "")
        self.pwd = os.getenv("SHOONYA_PWD", "")
        self.factor2 = os.getenv("SHOONYA_FACTOR2", "")
        self.vc = os.getenv("SHOONYA_VC", "")
        self.app_key = os.getenv("SHOONYA_APP_KEY", "")
        self.imei = os.getenv("SHOONYA_IMEI", "abc1234")
        
        ws_url = self.host.replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP')
        if not ws_url.endswith('/'): ws_url += '/'
        
        self.active_tokens = {
            "26000": "NIFTY50",   # Hardcode NSE indices as fallbacks
            "26001": "BANKNIFTY", 
            "1": "SENSEX",        # BSE Index
            "1333": "HDFCBANK",   # Power 10 Heavyweights
            "4963": "ICICIBANK",
            "1594": "INFY",
            "11536": "TCS",
            "1660": "ITC",
            "3045": "SBIN",
            "5900": "AXISBANK",
            "1922": "KOTAKBANK",
            "11483": "LT",
            "2885": "RELIANCE"
        }
        
        # [Audit 13.2] Initialize NorenApi with explicit host/websocket
        self.api = NorenApi(host=self.host, websocket=ws_url)
        
        # Shared Memory Setup
        self.shm = TickSharedMemory(create=True)

        # [Audit-Fix] Component 3: JIT Rate Limiter for search_scrip
        self.search_rate_limiter = TokenBucketRateLimiter(rate=1, per=1.0)
        self.sequence_id = 0 # [Audit-Fix] Monotonic counter for temporal precision
        
        # [Audit-Fix] Component 7: Feed Watchdog (Liveliness Trap)
        self.last_feed_time = {"NIFTY50": time.time(), "BANKNIFTY": time.time()}
        self._day_highs = {}
        self._day_lows = {}

    def event_handler_feed_update(self, tick_data):
        """Callback fired by Shoonya WebSocket on every price tick."""
        try:
            if 'lp' not in tick_data:
                return # Ignore non-price updates

            token = tick_data.get('tk')
            if token not in self.active_tokens:
                # Try to reverse lookup NFO token if we dynamically subscribed to it
                sym = get_symbol(token)
                if sym:
                    self.active_tokens[token] = sym
                else:
                    return
                
            symbol = self.active_tokens[token]
            price = float(tick_data['lp'])
            volume = int(tick_data.get('v', 0))
            
            # Persistent high/low guard
            curr_high = float(tick_data.get('h', self._day_highs.get(symbol, price)))
            curr_low = float(tick_data.get('l', self._day_lows.get(symbol, price)))
            self._day_highs[symbol] = max(curr_high, price)
            self._day_lows[symbol] = min(curr_low, price)
            
            # [Audit-Fix] Component 1 & 2: Latency Anchor (LTT) and Institutional Flow (OI)
            exchange_ts = tick_data.get('ltt') # Last Traded Time from Shoonya
            oi = int(tick_data.get('oi', 0))

            # Latency Calculation (Current Time - Last Traded Time)
            now_ts = time.time()
            feed_time = float(tick_data.get('ft', now_ts))
            latency_ms = (now_ts - feed_time) * 1000

            # [Audit-Fix] Component 6: Temporal Precision (Sequence ID)
            self.sequence_id += 1
            
            # [Audit-Fix] Feed Watchdog: Update arrival time for core indices
            if symbol in self.last_feed_time:
                self.last_feed_time[symbol] = now_ts
            
            # Simplified payload matching our internal microservice format
            payload = {
                "symbol": symbol,
                "price": price,
                "volume": volume,
                "oi": oi,
                "exchange_ts": exchange_ts,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence_id": self.sequence_id,
                "latency_ms": max(0, latency_ms),
                "day_high": self._day_highs[symbol],
                "day_low": self._day_lows[symbol],
                "type": "TICK"
            }
            
            # The Shoonya library uses a background thread for WebSockets.
            # We must dispatch to our asyncio event loop to interact with Redis/ZeroMQ safely.
            asyncio.run_coroutine_threadsafe(
                self._broadcast_tick(symbol, payload),
                self.loop
            )
            
        except Exception as e:
            logger.error(f"Error handling feed update: {e}")

    async def _broadcast_tick(self, symbol, payload):
        """Asynchronously blasts the tick data to Redis, Shared Memory, and ZeroMQ."""
        # 1. Shared Memory Zero-Copy Write
        if symbol in SYMBOL_TO_SLOT:
            slot = SYMBOL_TO_SLOT[symbol]
            # [Audit-Fix] Component 4: SHM Heartbeat Synchronization
            # Pass high-res microsecond epoch for staleness detection
            hires_ts = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
            self.shm.write_tick(slot, symbol, payload['price'], payload['volume'], 
                                timestamp=datetime.fromisoformat(payload['timestamp']).timestamp(),
                                hires_ts=hires_ts,
                                sequence_id=payload['sequence_id'],
                                high=payload['day_high'],
                                low=payload['day_low'])

        # 2. Update latest state for Dashboard API (Legacy/Fallback)
        await self.redis.set(f"latest_tick:{symbol}", json.dumps(payload))
        
        # --- Recommendation 3: Redis Time-Travel Buffer ---
        # Store last 100 ticks for quick engine recovery
        list_key = f"tick_buffer:{symbol}"
        await self.redis.lpush(list_key, json.dumps(payload))
        await self.redis.ltrim(list_key, 0, 99)
        
        # 3. Push directly to ZeroMQ (Brokerless) for Strategy Engine (Event trigger)
        topic = f"TICK.{symbol}"
        # We can now just send the topic or a minimal payload since data is in SHM
        await self.mq_manager.send_json(self.pub_socket, topic, payload)
        
        logger.debug(f"Broadcasted live {symbol} @ {payload['price']} [SHM Slot {SYMBOL_TO_SLOT.get(symbol)}]")

    def open_callback(self):
        """Callback fired when WebSocket opens."""
        logger.info("Shoonya WebSocket Opened.")
        
        # 1. Subscribe to base indices and Power 10 (NSE & BSE)
        base_tokens = [
            "NSE|26000", "NSE|26001", "BSE|1",
            "NSE|1333", "NSE|4963", "NSE|1594", "NSE|11536", "NSE|1660",
            "NSE|3045", "NSE|5900", "NSE|1922", "NSE|11483", "NSE|2885"
        ]
        for exch_token in base_tokens:
            self.api.subscribe(exch_token)
            logger.info(f"Subscribed to Base {exch_token}")
            
        # 2. Dynamically fetch deployed symbols from Redis, resolve NFO tokens, and subscribe
        async def _subscribe_dynamic():
            try:
                # [Audit-Fix] Reboot Integrity: Guard against "Ghost Positions"
                # A. Scan active strategies for planned deployments
                active_strats = await self.redis.hgetall("active_strategies")
                for strat_id_b, config_b in active_strats.items():
                    config = json.loads(config_b)
                    if config.get("enabled", True):
                        for symbol in config.get("symbols", []):
                            token, exch = await self._resolve_symbol_to_token(symbol)
                            if token and exch:
                                self.api.subscribe(f"{exch}|{token}")
                                self.active_tokens[token] = symbol
                                logger.info(f"Subscribed to Dynamic {exch}|{token} for {symbol}")

                # B. Scan broker-reported truth map for already held positions (Reboot Safety)
                held_positions = await self.redis.hgetall("broker_positions")
                if not held_positions:
                    logger.warning("⚠️ REBOOT INTEGRITY: 'broker_positions' not found in Redis. Skipping position-aware subscription.")
                else:
                    for symbol, qty_str in held_positions.items():
                        try:
                            if int(qty_str) != 0:
                                token, exch = await self._resolve_symbol_to_token(symbol)
                                if token and exch:
                                    self.api.subscribe(f"{exch}|{token}")
                                    self.active_tokens[token] = symbol
                                    logger.info(f"🛡️ REBOOT INTEGRITY: Restored subscription for held position {symbol} ({exch}|{token})")
                        except Exception as e:
                            logger.error(f"Failed to restore subscription for position {symbol}: {e}")

            except Exception as e:
                logger.error(f"Error subscribing to dynamic tokens: {e}")
                
        asyncio.run_coroutine_threadsafe(_subscribe_dynamic(), self.loop)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        logger.info("Authenticating with Shoonya APIs via NorenRest...")
        
        totp = pyotp.TOTP(self.factor2).now()
        
        res = self.api.login(
            userid=self.user, 
            password=self.pwd, 
            twoFA=totp, 
            vendor_code=self.vc, 
            api_secret=self.app_key, 
            imei=self.imei
        )
        
        if res and res.get('stat') == 'Ok':
            logger.info(f"Successfully logged in as {res.get('uname')}")
            
            logger.info("Starting WebSockets feed...")
            # [Audit 11.1] Heartbeat integration
            self.hb = HeartbeatProvider("ShoonyaGateway", self.redis)
            asyncio.create_task(self.hb.run_heartbeat())
            
            asyncio.create_task(send_cloud_alert("🌐 SHOONYA GATEWAY: Active and streaming live market data.", alert_type="SYSTEM"))
            
            # 2. Start Feed Watchdog for market-hours liveliness monitoring
            asyncio.create_task(self._feed_watchdog_loop())

            self.api.start_websocket(
                order_update_callback=lambda x: None, 
                subscribe_callback=self.event_handler_feed_update, 
                socket_open_callback=self.open_callback
            )
            
            # 3. Start JIT Subscription Listener for Liquidation/Strategy engines
            asyncio.create_task(self._dynamic_subscription_listener())
            
            # Keep alive and monitor (Audit 9.1)
            self._shutdown_event = asyncio.Event()
            logger.info("Gateway keep-alive active. Monitoring WebSocket...")
            try:
                await self._shutdown_event.wait()
            except asyncio.CancelledError:
                logger.info("Shutdown event cancelled.")
            finally:
                self.api.close_websocket()
        else:
            logger.error(f"Failed to authenticate with Shoonya. Halting Data Gateway. Details: {res}")

    async def _dynamic_subscription_listener(self):
        """
        Listens to Redis 'dynamic_subscriptions' channel for JIT requests.
        Format: 'EXCH|TOKEN' (e.g., 'NFO|54321') or just 'SYMBOL' (e.g., 'NIFTY24MAR22500CE')
        """
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("dynamic_subscriptions")
        logger.info("JIT Subscription Listener active for Liquidation & Strategy engines.")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    raw_data = message["data"].decode('utf-8') if isinstance(message["data"], bytes) else message["data"]
                    logger.info(f"⚡ JIT Subscription Request Received: {raw_data}")
                    
                    # Support both legacy string-only and new JSON-action formats
                    action = "subscribe"
                    data = raw_data
                    
                    try:
                        json_msg = json.loads(raw_data)
                        if isinstance(json_msg, dict) and "action" in json_msg:
                            action = json_msg["action"].lower()
                            data = json_msg.get("symbol") or json_msg.get("data")
                    except json.JSONDecodeError:
                        pass # Carry on with legacy format
                    
                    if not data:
                        logger.warning("Empty data in JIT message")
                        continue

                    if action == "subscribe":
                        if "|" in data:
                            self.api.subscribe(data)
                        else:
                            token, exch = await self._resolve_symbol_to_token(data)
                            if token and exch:
                                self.api.subscribe(f"{exch}|{token}")
                                self.active_tokens[token] = data
                                logger.info(f"Subscribed to {data} ({exch}|{token})")
                    
                    elif action == "unsubscribe":
                        exch_token = data if "|" in data else None
                        if not exch_token:
                            token, exch = await self._resolve_symbol_to_token(data)
                            if token and exch:
                                exch_token = f"{exch}|{token}"
                        
                        if exch_token:
                            self.api.unsubscribe(exch_token)
                            # Remove from active_tokens for cleanup
                            token = exch_token.split("|")[-1]
                            self.active_tokens.pop(token, None)
                            logger.info(f"🗑️ Unsubscribed from {data} ({exch_token})")
                            
                except Exception as e:
                    logger.error(f"Error in JIT subscription listener: {e}")

    async def _feed_watchdog_loop(self):
        """
        Monitors NIFTY50 and BANKNIFTY feed arrival.
        If data stalls for > 2s during market hours, forces a reconnect.
        """
        logger.info("🛡️ FEED WATCHDOG: Monitoring Nifty/BankNifty liveliness (2s threshold).")
        while True:
            await asyncio.sleep(1)
            
            if not self._is_market_hours():
                continue
                
            now = time.time()
            for symbol, last_time in self.last_feed_time.items():
                staleness = now - last_time
                if staleness > 2.0:
                    logger.warning(f"⚠️ FEED_STALLED: {symbol} has not ticked in {staleness:.1f}s. Forcing WebSocket RECONNECT.")
                    # Trigger Shoonya library's internal reconnect by closing
                    try:
                        self.api.close_websocket()
                    except Exception as e:
                        logger.error(f"Failed to close websocket during stall: {e}")
                    
                    # Reset timestamps to avoid immediate re-trigger during connection attempt
                    for s in self.last_feed_time:
                        self.last_feed_time[s] = now
                    break

    def _is_market_hours(self) -> bool:
        """Returns True if current time is within Indian Market Hours (09:15-15:30 IST)."""
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        # Market open: 09:15, close: 15:30
        if now_ist.weekday() >= 5: # Saturday/Sunday
            return False
            
        market_start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
        market_end = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
        
        return market_start <= now_ist <= market_end

    def _get_exchange_for_symbol(self, symbol: str) -> str:
        """Heuristic to determine exchange based on symbol pattern."""
        if symbol.startswith("BSE:"):
            return "BSE"
        if any(idx in symbol for idx in ["SENSEX", "BANKEX"]):
            # If it has a date/strike (e.g. SENSEX26MAR72000CE), it's BFO
            # If it's just 'SENSEX', it's BSE (Index) - Note: Shoonya Index TSym is often just 'SENSEX'
            if any(char.isdigit() for char in symbol):
                return "BFO"
            return "BSE"
        if any(s in symbol for s in ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "LT"]):
            return "NSE"
        # Standard NSE options are NFO
        if any(idx in symbol for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]):
            return "NFO"
        return "NFO" # Default to NFO for most F&O strings

    async def _resolve_local_token(self, symbol: str) -> tuple[str | None, str | None]:
        """Helper to resolve token from local Redis/Master file via executor."""
        exch = self._get_exchange_for_symbol(symbol)
        token = await self.loop.run_in_executor(None, lambda: get_token(symbol, exchange=exch))
        return token, exch

    async def _resolve_symbol_to_token(self, symbol: str) -> tuple[str | None, str | None]:
        """Resolves a trading symbol to a token and exchange using Shoonya search_scrip with JIT Protection."""
        # [Audit-Fix] Component 3b: Local/Redis Token Cache
        cached_data = await self.redis.hget("token_lookup", symbol)
        if cached_data:
            return tuple(cached_data.split("|"))
        
        # Try local master first
        token, exch = await self._resolve_local_token(symbol)
        if token and exch:
            # Cache it
            await self.redis.hset("token_lookup", symbol, f"{token}|{exch}")
            return token, exch

        # If local master fails, try API search
        exch = self._get_exchange_for_symbol(symbol)
        if symbol.startswith("BSE:"):
            symbol = symbol.replace("BSE:", "")
            
        try:
            # [Audit-Fix] Component 3a: JIT Rate Limiting for API key safety
            await self.search_rate_limiter.acquire()
            
            # For options, symbols are like 'NIFTY26MAR22350CE'
            # For stocks, they are plain 'RELIANCE'
            res = await self.loop.run_in_executor(None, lambda: self.api.search_scrip(exchange=exch, searchtext=symbol))
            
            if res and res.get('stat') == 'Ok':
                values = res.get('values', [])
                if values:
                    # Exact match check
                    for val in values:
                        if val.get('tsym') == symbol:
                            token, out_exch = val.get('token'), val.get('exch')
                            # Cache it
                            await self.redis.hset("token_lookup", symbol, f"{token}|{out_exch}")
                            return token, out_exch
                    
                    # Fallback to first result
                    token, out_exch = values[0].get('token'), values[0].get('exch')
                    await self.redis.hset("token_lookup", symbol, f"{token}|{out_exch}")
                    return token, out_exch
        except Exception as e:
            logger.error(f"Search scrip failed for {symbol}: {e}")
        return None, None

async def main():
    logger.info("Starting Shoonya Live Data Gateway...")
    
    # Connect to Data Vault & ZeroMQ
    redis_client = redis.from_url(get_redis_url(), decode_responses=True)
    mq = MQManager()
    pub_socket = mq.create_publisher(Ports.MARKET_DATA)
    
    streamer = ShoonyaDataStreamer(redis_client, pub_socket)
    
    try:
        await streamer.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        pub_socket.close()
        await redis_client.aclose()
        mq.context.term()

if __name__ == "__main__":
    # [Audit-Fix] Component 4: Core 1 Pinning for Nervous System minimization
    try:
        import os
        if hasattr(os, 'sched_setaffinity'):
            os.sched_setaffinity(0, {1})
            logger.info("🎯 CORE PINNING: ShoonyaGateway pinned to Core 1.")
    except Exception as e:
        logger.warning(f"Could not pin to Core 1: {e}")

    if uvloop:
        uvloop.install()
    elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
