import asyncio
import json
import logging
import os
import hashlib
from datetime import datetime, timezone
import redis.asyncio as redis
from dotenv import load_dotenv

from core.mq import MQManager, Ports
from NorenRestApiPy.NorenApi import NorenApi

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ShoonyaGateway")

# Mapped identifiers (token to symbol for Shoonya NSE)
# In production, this dictionary would be built dynamically by searching NorenApi for tokens
TOKEN_MAP = {
    "26000": "NIFTY50",
    "26001": "BANKNIFTY", 
    "2885": "RELIANCE"
}

class ShoonyaDataStreamer:
    def __init__(self, redis_client, pub_socket):
        self.redis = redis_client
        self.pub_socket = pub_socket
        self.mq_manager = MQManager()
        
        load_dotenv()
        self.host = os.getenv("SHOONYA_HOST")
        self.user = os.getenv("SHOONYA_USER")
        self.pwd = os.getenv("SHOONYA_PWD")
        self.factor2 = os.getenv("SHOONYA_FACTOR2")
        self.vc = os.getenv("SHOONYA_VC")
        self.app_key = os.getenv("SHOONYA_APP_KEY")
        self.imei = os.getenv("SHOONYA_IMEI", "abc1234")
        
        ws_url = self.host.replace('https', 'wss').replace('NorenWClientTP', 'NorenWSTP')
        if not ws_url.endswith('/'): ws_url += '/'
        
        self.api = NorenApi(host=self.host, websocket=ws_url)

    def event_handler_feed_update(self, tick_data):
        """Callback fired by Shoonya WebSocket on every price tick."""
        try:
            if 'lp' not in tick_data:
                return # Ignore non-price updates

            token = tick_data.get('tk')
            if token not in TOKEN_MAP:
                return
                
            symbol = TOKEN_MAP[token]
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
        """Asynchronously blasts the tick data to Redis and ZeroMQ."""
        # 1. Update latest state for Dashboard API
        await self.redis.set(f"latest_tick:{symbol}", json.dumps(payload))
        
        # 2. Push directly to ZeroMQ (Brokerless) for Strategy Engine
        topic = f"TICK.{symbol}"
        await self.mq_manager.send_json(self.pub_socket, payload, topic=topic)
        
        logger.debug(f"Broadcasted live {symbol} @ {payload['price']}")

    def open_callback(self):
        """Callback fired when WebSocket opens."""
        logger.info("Shoonya WebSocket Opened.")
        # Subscribe to tokens
        for token in TOKEN_MAP.keys():
            self.api.subscribe(f"NSE|{token}")
            logger.info(f"Subscribed to NSE|{token}")

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
    if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
