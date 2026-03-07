import zmq
import zmq.asyncio
import json
import logging

class MQManager:
    """Wrapper around ZeroMQ for async pub/sub and push/pull messaging."""
    
    def __init__(self):
        self.context = zmq.asyncio.Context()
        self.logger = logging.getLogger("MQ")
        # Ensure we bind to local interfaces
        self.host = "127.0.0.1"

    def create_publisher(self, port: int):
        """Creates an async Publisher socket bound to the given port."""
        socket = self.context.socket(zmq.PUB)
        socket.bind(f"tcp://{self.host}:{port}")
        self.logger.info(f"Publisher bound to port {port}")
        return socket

    def create_subscriber(self, port: int, topics: list = [""]):
        """Creates an async Subscriber socket connected to the given port."""
        socket = self.context.socket(zmq.SUB)
        socket.connect(f"tcp://{self.host}:{port}")
        for topic in topics:
            if isinstance(topic, str):
                socket.setsockopt_string(zmq.SUBSCRIBE, topic)
            else:
                socket.setsockopt(zmq.SUBSCRIBE, topic)
        self.logger.info(f"Subscriber connected to port {port} with topics {topics}")
        return socket

    def create_push(self, port: int):
        """Creates an async Push socket for specific task execution commands."""
        socket = self.context.socket(zmq.PUSH)
        socket.bind(f"tcp://{self.host}:{port}")
        self.logger.info(f"Push socket bound to port {port}")
        return socket

    def create_pull(self, port: int):
        """Creates an async Pull socket to receive task execution commands."""
        socket = self.context.socket(zmq.PULL)
        socket.connect(f"tcp://{self.host}:{port}")
        self.logger.info(f"Pull socket connected to port {port}")
        return socket

    async def send_json(self, socket, data: dict, topic: str = None):
        """Helper to send JSON payloads, optionally with a topic prefix for Pub/Sub."""
        payload = json.dumps(data)
        if topic:
            await socket.send_string(f"{topic} {payload}")
        else:
            await socket.send_string(payload)

    async def recv_json(self, socket):
        """Helper to receive JSON payloads. Returns (topic, data) for Sub sockets."""
        message = await socket.recv_string()
        # If it starts with { or [, it's raw JSON without a topic
        if message.startswith("{") or message.startswith("["):
            try:
                return None, json.loads(message)
            except json.JSONDecodeError:
                return None, message

        # If there's a space, we assume it's topic + payload (PUB/SUB)
        if " " in message:
            parts = message.split(" ", 1)
            try:
                return parts[0], json.loads(parts[1])
            except json.JSONDecodeError:
                return parts[0], parts[1]
                
        # Otherwise just payload (PUSH/PULL or topic-less)
        try:
            return None, json.loads(message)
        except json.JSONDecodeError:
            return None, message

from datetime import datetime
import redis.asyncio as redis

# Common Ports Configuration
class Ports:
    MARKET_DATA = 5555  # Data Gateway publishes here
    ORDERS = 5556       # Strategy Engine pushes orders here, Paper Bridge pulls
    TRADE_EVENTS = 5557 # Paper Bridge publishes executions here

class RedisLogger:
    """Streams logs to Redis for dashboard visibility."""
    def __init__(self, redis_url="redis://localhost:6379", key="live_logs", max_len=100):
        self.redis = redis.from_url(redis_url)
        self.key = key
        self.max_len = max_len

    async def log(self, message, level="INFO"):
        log_entry = json.dumps({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message
        })
        try:
            await self.redis.lpush(self.key, log_entry)
            await self.redis.ltrim(self.key, 0, self.max_len - 1)
        except Exception:
            pass
