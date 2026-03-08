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

    def create_publisher(self, port: int, bind: bool = True):
        """Creates an async Publisher socket."""
        socket = self.context.socket(zmq.PUB)
        addr = f"tcp://{self.host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Publisher bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Publisher connected to {addr}")
        return socket

    def create_subscriber(self, port: int, topics: list = [""], bind: bool = False):
        """Creates an async Subscriber socket."""
        socket = self.context.socket(zmq.SUB)
        addr = f"tcp://{self.host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Subscriber bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Subscriber connected to {addr}")
            
        for topic in topics:
            if isinstance(topic, str):
                socket.setsockopt_string(zmq.SUBSCRIBE, topic)
            else:
                socket.setsockopt(zmq.SUBSCRIBE, topic)
        return socket

    def create_push(self, port: int, bind: bool = True):
        """Creates an async Push socket."""
        socket = self.context.socket(zmq.PUSH)
        addr = f"tcp://{self.host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Push socket bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Push socket connected to {addr}")
        return socket

    def create_pull(self, port: int, bind: bool = False):
        """Creates an async Pull socket."""
        socket = self.context.socket(zmq.PULL)
        addr = f"tcp://{self.host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Pull socket bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Pull socket connected to {addr}")
        return socket

    def create_dealer(self, port: int, identity: bytes | None = None, bind: bool = False):
        """Creates an async DEALER socket (pairs with ROUTER for decoupled async IPC)."""
        socket = self.context.socket(zmq.DEALER)
        if identity:
            socket.identity = identity
        addr = f"tcp://{self.host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Dealer socket bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Dealer socket connected to {addr}")
        return socket

    def create_router(self, port: int, bind: bool = True):
        """Creates an async ROUTER socket (accepts DEALER connections for decoupled dispatch)."""
        socket = self.context.socket(zmq.ROUTER)
        addr = f"tcp://{self.host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Router socket bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Router socket connected to {addr}")
        return socket

    async def send_dealer(self, socket, data: dict, topic: str = None):
        """Send via DEALER socket (prepends empty delimiter frame for ROUTER compat)."""
        payload = json.dumps(data)
        msg = f"{topic} {payload}" if topic else payload
        await socket.send_multipart([b"", msg.encode()])

    async def recv_router(self, socket):
        """Receive via ROUTER socket. Returns (identity, topic, data)."""
        frames = await socket.recv_multipart()
        # Frames: [identity, empty_delimiter, payload]
        identity = frames[0]
        payload_raw = frames[-1].decode()
        if " " in payload_raw:
            parts = payload_raw.split(" ", 1)
            try:
                return identity, parts[0], json.loads(parts[1])
            except json.JSONDecodeError:
                return identity, parts[0], parts[1]
        try:
            return identity, None, json.loads(payload_raw)
        except json.JSONDecodeError:
            return identity, None, payload_raw

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
    MARKET_DATA = 5555    # Data Gateway publishes TICK_DATA
    ORDERS = 5556         # Strategy Engine pushes ORDER_INTENT
    TRADE_EVENTS = 5557   # Execution Bridge publishes FILL_RECEIPT
    MARKET_STATE = 5558   # Market Sensor publishes MARKET_STATE vector
    SYSTEM_CMD = 5559     # Meta Router publishes SYSTEM_CMD
    LOGGING = 5560        # Centralized logging
    RECONCILER = 5561     # Order Reconciler ROUTER (receives DEALER intents)
    SYSTEM_CTRL = 5562    # System Controller broadcasts (lifecycle events)

class Topics:
    TICK_DATA = "TICK"
    MARKET_STATE = "STATE"
    SYSTEM_CMD = "CMD"
    ORDER_INTENT = "INTENT"
    FILL_RECEIPT = "FILL"

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
