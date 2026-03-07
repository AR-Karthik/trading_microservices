import asyncio
import logging
import uuid
import collections
from datetime import datetime, timezone
from core.mq import MQManager, Ports

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("StrategyEngine")

# Simple strategy parameters
SMA_PERIOD = 10
state = collections.defaultdict(lambda: collections.deque(maxlen=SMA_PERIOD))
positions = collections.defaultdict(int)

async def run_strategy(sub_socket, push_socket, mq_manager):
    """
    Subscribes to market data, runs a simple moving average crossover strategy,
    and pushes structured orders to the execution layer.
    """
    logger.info("Starting Strategy Engine loop...")
    
    while True:
        try:
            # 1. Brokerless Routing: Zero latency pub/sub consumption
            topic, tick = await mq_manager.recv_json(sub_socket)
            
            if not tick:
                continue

            symbol = tick.get("symbol")
            price = tick.get("price")
            
            # 2. Simple Inference Engine (e.g., Simple Moving Average)
            history = state[symbol]
            history.append(price)
            
            if len(history) < SMA_PERIOD:
                continue # Need more data for SMA
                
            sma = sum(history) / SMA_PERIOD
            
            signal = None
            current_position = positions[symbol]
            
            # Simple mean reversion / crossover logic
            if price > sma * 1.0005 and current_position <= 0:
                signal = "BUY"
                positions[symbol] = 1
            elif price < sma * 0.9995 and current_position >= 0:
                signal = "SELL"
                positions[symbol] = -1
                
            if signal:
                # 3. Create Actionable Order
                order = {
                    "order_id": str(uuid.uuid4()),
                    "symbol": symbol,
                    "action": signal,
                    "quantity": 100, # Fixed qty for paper trading
                    "order_type": "MARKET",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "price": price,   # Reference price for slippage calculation
                    "strategy_id": "SMA_CROSSOVER_V1"
                }
                
                # Push order to execution engine via ZeroMQ PUSH socket
                await mq_manager.send_json(push_socket, order)
                logger.info(f"Signal Generated: {signal} {symbol} @ {price:.2f} (SMA: {sma:.2f})")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in strategy loop: {e}")
            await asyncio.sleep(1)

async def start_engine():
    """Initializes the Strategy Engine service."""
    logger.info("Initializing Strategy Engine...")

    # Ensure event loop compatibility for ZeroMQ
    if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Initialize ZeroMQ Messaging
    mq = MQManager()
    
    # Subscribe to all market data ticks
    sub_socket = mq.create_subscriber(Ports.MARKET_DATA, topics=["TICK."])
    
    # Push socket to send orders to Paper Bridge
    push_socket = mq.create_push(Ports.ORDERS)

    try:
        await run_strategy(sub_socket, push_socket, mq)
    except KeyboardInterrupt:
        logger.info("Shutting down Strategy Engine.")
    finally:
        sub_socket.close()
        push_socket.close()
        mq.context.term()

if __name__ == "__main__":
    try:
        if hasattr(asyncio, 'WindowsSelectorEventLoopPolicy'):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(start_engine())
    except KeyboardInterrupt:
        pass
