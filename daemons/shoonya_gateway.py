import asyncio
import json
import logging
import os
import hashlib
from datetime import datetime, timezone
import redis.asyncio as redis
from dotenv import load_dotenv

from core.mq import MQManager, Ports, RedisLogger
from core.health import Heartbeat
from core.alerts import send_cloud_alert
from NorenRestApiPy.NorenApi import NorenApi

try:
    import uvloop
except ImportError:
    uvloop = None

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.shoonya_master import get_token, get_symbol

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ShoonyaGateway")

class ShoonyaDataStreamer:
    def __init__(self, redis_client, pub_socket):
        self.redis = redis_client
        self.pub_socket = pub_socket
        self.mq_manager = MQManager()
        
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
            "2885": "RELIANCE"
        }
        
        # Shared Memory Setup
        self.shm = TickSharedMemory(create=True)
        self.symbol_slots = {}
        self.next_slot = 0

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
            
            # Simplified payload matching our internal microservice format
            payload = {
                "symbol": symbol,
                "price": price,
                "volume": volume,
                # In production we would track OI here if trading derivatives
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
            # Offload blocking write to thread pool if needed, but struct.pack is fast
            self.shm.write_tick(slot, symbol, payload['price'], payload['volume'])

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
        await self.mq_manager.send_json(self.pub_socket, payload, topic=topic)
        
        logger.debug(f"Broadcasted live {symbol} @ {payload['price']} [SHM Slot {self.symbol_slots.get(symbol)}]")

    def open_callback(self):
        """Callback fired when WebSocket opens."""
        logger.info("Shoonya WebSocket Opened.")
        
        # 1. Subscribe to base indices (NSE)
        for token in ["26000", "26001", "2885"]:
            self.api.subscribe(f"NSE|{token}")
            logger.info(f"Subscribed to Base NSE|{token}")
            
        # 2. Dynamically fetch deployed symbols from Redis, resolve NFO tokens, and subscribe
        try:
            active_strats = self.redis.hgetall("active_strategies")
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

    async def start(self):
        self.loop = asyncio.get_running_loop()
        logger.info("Authenticating with Shoonya APIs via NorenRest...")
        
        # Required SHA256 conversion for password and appkey (Noren requirement for standard login loop)
        pwd = hashlib.sha256(self.pwd.encode('utf-8')).hexdigest()
        u_app_key = '{0}|{1}'.format(self.user, self.app_key)
        app_key = hashlib.sha256(u_app_key.encode('utf-8')).hexdigest()
        
        res = self.api.login(
            userid=self.user, 
            password=pwd, 
            twoFA=self.factor2, 
            vendor_code=self.vc, 
            api_secret=app_key, 
            imei=self.imei
        )
        
        if res and res.get('stat') == 'Ok':
            logger.info(f"Successfully logged in as {res.get('uname')}")
            
            logger.info("Starting WebSockets feed...")
            asyncio.create_task(send_cloud_alert("🌐 SHOONYA GATEWAY: Active and streaming live market data.", alert_type="SYSTEM"))
            self.api.start_websocket(
                order_update_callback=lambda x: None, 
                subscribe_callback=self.event_handler_feed_update, 
                socket_open_callback=self.open_callback
            )
            
            # Keep alive
            while True:
                await asyncio.sleep(60)
        else:
            logger.error(f"Failed to authenticate with Shoonya. Halting Data Gateway. Details: {res}")

async def main():
    logger.info("Starting Shoonya Live Data Gateway...")
    
    # Connect to Data Vault & ZeroMQ
    redis_client = redis.Redis(host='localhost', port=6379, db=0)
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
    if uvloop:
        uvloop.install()
    elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
