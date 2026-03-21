import asyncio
import json
import logging
import os
import hashlib
import pyotp
from datetime import datetime, timezone
import redis.asyncio as redis
from dotenv import load_dotenv

from core.mq import MQManager, Ports, RedisLogger
from core.health import HeartbeatProvider
from core.alerts import send_cloud_alert
from NorenRestApiPy.NorenApi import NorenApi
from core.shared_memory import TickSharedMemory, MAX_SLOTS

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
        self.last_refill = asyncio.get_event_loop().time()
    
    async def acquire(self):
        while self.tokens < 1:
            await asyncio.sleep(0.1)
            now = asyncio.get_event_loop().time()
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
        self.symbol_slots = {}
        self.next_slot = 0

        # [Audit-Fix] Component 3: JIT Rate Limiter for search_scrip
        self.search_rate_limiter = TokenBucketRateLimiter(rate=1, per=1.0)

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
            
            # [Audit-Fix] Component 1 & 2: Latency Anchor (LTT) and Institutional Flow (OI)
            exchange_ts = tick_data.get('ltt') # Last Traded Time from Shoonya
            oi = int(tick_data.get('oi', 0))

            # Simplified payload matching our internal microservice format
            payload = {
                "symbol": symbol,
                "price": price,
                "volume": volume,
                "oi": oi,
                "exchange_ts": exchange_ts,
                "timestamp": datetime.now(timezone.utc).isoformat(),
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
        if symbol not in self.symbol_slots:
            if self.next_slot < MAX_SLOTS:
                self.symbol_slots[symbol] = self.next_slot
                await self.redis.hset("shm_slots", symbol, self.next_slot)
                self.next_slot += 1
            else:
                logger.warning(f"Shared memory full! Cannot allocate slot for {symbol}")
        
        if symbol in self.symbol_slots:
            slot = self.symbol_slots[symbol]
            # [Audit-Fix] Component 4: SHM Heartbeat Synchronization
            # Pass high-res microsecond epoch for staleness detection
            hires_ts = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
            self.shm.write_tick(slot, symbol, payload['price'], payload['volume'], 
                                timestamp=datetime.fromisoformat(payload['timestamp']).timestamp(),
                                hires_ts=hires_ts)

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
        
        logger.debug(f"Broadcasted live {symbol} @ {payload['price']} [SHM Slot {self.symbol_slots.get(symbol)}]")

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
                active_strats = await self.redis.hgetall("active_strategies")
                for strat_id_b, config_b in active_strats.items():
                    config = json.loads(config_b)
                    if config.get("enabled", True):
                        for symbol in config.get("symbols", []):
                            # Example: If a strategy deployed "NIFTY24FEB26C22000"
                            resolved_token = get_token(symbol)
                            if resolved_token:
                                self.api.subscribe(f"NFO|{resolved_token}")
                                self.active_tokens[resolved_token] = symbol
                                logger.info(f"Subscribed to Dynamic NFO|{resolved_token} for {symbol}")
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
                    data = message["data"].decode('utf-8') if isinstance(message["data"], bytes) else message["data"]
                    logger.info(f"⚡ JIT Subscription Request Received: {data}")
                    
                    if "|" in data:
                        # Direct exchange|token format
                        exch, token = data.split("|")
                        self.api.subscribe(data)
                        # We don't always know the symbol here, so we might need a lookup
                        # but Shoonya uses tokens in its 'tk' field anyway.
                    else:
                        # Symbol format - resolve to token
                        token, exch = await self._resolve_symbol_to_token(data)
                        if token and exch:
                            self.api.subscribe(f"{exch}|{token}")
                            self.active_tokens[token] = data
                            logger.info(f"Subscribed to {data} ({exch}|{token})")
                        else:
                            logger.warning(f"Could not resolve token for symbol: {data}")
                            
                except Exception as e:
                    logger.error(f"Error in JIT subscription listener: {e}")

    async def _resolve_symbol_to_token(self, symbol: str) -> tuple[str | None, str | None]:
        """Resolves a trading symbol to a token and exchange using Shoonya search_scrip with JIT Protection."""
        # [Audit-Fix] Component 3b: Local/Redis Token Cache
        cached_data = await self.redis.hget("token_lookup", symbol)
        if cached_data:
            return tuple(cached_data.split("|"))

        # [Audit-Fix] Component 5: Robust BFO/NSE Routing
        exch = "NFO"
        if any(idx in symbol for idx in ["SENSEX", "BANKEX"]): 
            exch = "BFO"
        elif any(s in symbol for s in ["RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "LT"]):
            exch = "NSE"
            
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
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_client = redis.Redis(host=redis_host, port=6379, db=0)
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
