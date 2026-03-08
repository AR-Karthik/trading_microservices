import asyncio
import json
import random
import logging
from datetime import datetime, timezone
import redis.asyncio as redis
from core.mq import MQManager, Ports, Topics

try:
    import uvloop
except ImportError:
    uvloop = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("DataGateway")

# Define symbols to mock
SYMBOLS = ["NIFTY50", "BANKNIFTY", "RELIANCE"]

async def broker_websocket_simulation(redis_client: redis.Redis, pub_socket):
    """
    Simulates a WebSocket connection to a broker.
    Generates dummy tick data with low latency characteristics.
    """
    logger.info("Starting simulated Broker WebSocket connection...")
    
    # Initial mock prices and OI
    prices = {
        "NIFTY50": 22000.0,
        "BANKNIFTY": 46000.0,
        "RELIANCE": 2900.0
    }
    oi = {s: 1000000 for s in SYMBOLS}
    
    while True:
        try:
            # Simulate high-frequency tick updates (every 50-200ms)
            await asyncio.sleep(random.uniform(0.05, 0.2))
            
            # Pick a random symbol to update
            symbol = random.choice(SYMBOLS)
            
            # Random walk price variation (+/- 0.05%)
            change = prices[symbol] * random.uniform(-0.0005, 0.0005)
            prices[symbol] = round(prices[symbol] + change, 2)
            
            # Random walk OI variation (+/- 0.1%)
            prev_symbol_oi = oi[symbol]
            oi_change = prev_symbol_oi * random.uniform(-0.001, 0.0015) # Slight bullish bias on OI
            oi[symbol] = int(prev_symbol_oi + oi_change)

            # Format tick payload
            tick_data = {
                "symbol": symbol,
                "price": prices[symbol],
                "volume": random.randint(1, 100),
                "oi": oi[symbol],
                "prev_oi": prev_symbol_oi,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": "TICK"
            }
            
            # 1. Ultra-fast Redis buffer for immediate retrieval by Web UI or other components
            # Store latest tick explicitly
            await redis_client.set(f"latest_tick:{symbol}", json.dumps(tick_data))
            
            # (Optional) we could keep a small Redis timeseries or list here too
            # e.g., await redis_client.lpush(f"history:{symbol}", json.dumps(tick_data))
            # await redis_client.ltrim(f"history:{symbol}", 0, 999) # Keep last 1000
            
            # 2. Brokerless Routing: ZeroMQ publish for microseconds latency to Strategy Engine
            mq_manager = MQManager()
            # Send with "TICK.SYMBOL" topic structure
            topic = f"TICK.{symbol}"
            await mq_manager.send_json(pub_socket, tick_data, topic=topic)
            
            logger.debug(f"Tick generated: {symbol} @ {prices[symbol]}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in websocket simulation loop: {e}")
            await asyncio.sleep(1)

async def start_gateway():
    """Initializes the gateway service."""
    logger.info("Initializing Data Gateway...")
    
    # Ensure event loop compatibility for ZeroMQ
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()) if isinstance(
        asyncio.get_event_loop_policy(), getattr(asyncio, 'WindowsProactorEventLoopPolicy', type(None))
    ) else None

    # Connect to Redis Data Vault
    redis_client = redis.Redis(host='localhost', port=6379, db=0)
    
    # Initialize ZeroMQ Messaging
    mq = MQManager()
    pub_socket = mq.create_publisher(Ports.MARKET_DATA)
    
    # Run the gateway loop
    try:
        await broker_websocket_simulation(redis_client, pub_socket)
    except KeyboardInterrupt:
        logger.info("Shutting down Data Gateway.")
    finally:
        pub_socket.close()
        await redis_client.aclose()
        mq.context.term()

if __name__ == "__main__":
    try:
        if uvloop:
            uvloop.install()
        elif hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(start_gateway())
    except KeyboardInterrupt:
        pass
