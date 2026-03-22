
import zmq
import zmq.asyncio
import asyncio
import json
import os

async def check():
    ctx = zmq.asyncio.Context()
    sub = ctx.socket(zmq.SUB)
    host = os.getenv("MQ_MARKET_DATA_HOST", "tick_sensor")
    addr = f"tcp://{host}:5555"
    print(f"Connecting to {addr}...")
    sub.connect(addr)
    sub.setsockopt_string(zmq.SUBSCRIBE, "TICK")
    
    try:
        print("Waiting for tick...")
        msg = await asyncio.wait_for(sub.recv_multipart(), timeout=10.0)
        print(f"Received message with {len(msg)} parts")
        for i, part in enumerate(msg):
            print(f"Part {i}: {part[:50]}...")
    except asyncio.TimeoutError:
        print("Timeout: No tick received in 10s")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        sub.close()
        ctx.term()

if __name__ == "__main__":
    asyncio.run(check())
