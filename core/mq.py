import zmq
import zmq.asyncio
import json
import logging
import os
import uuid
import contextvars
from datetime import datetime

try:
    import numpy as np
except ImportError:
    np = None

correlation_id_ctx = contextvars.ContextVar("correlation_id", default=None)

class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if np:
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
        return super().default(obj)


class MQManager:
    """Wrapper around ZeroMQ for async pub/sub and push/pull messaging."""
    
    def __init__(self):
        self.context = zmq.asyncio.Context()
        # Set a global receive timeout of 5 seconds for all sockets created from this context
        # Actually, ZMQ context doesn't have a default RCVTIMEO, so we'll set it in create methods
        self.logger = logging.getLogger("MQ")
        # In Docker, bind to 0.0.0.0 to be reachable from other containers
        self.host = "0.0.0.0"
        self.default_host = os.getenv("MQ_DEFAULT_HOST", "127.0.0.1")
        self.mq_hosts = {
            "market_data": os.getenv("MQ_MARKET_DATA_HOST", self.default_host),
            "orders": os.getenv("MQ_ORDERS_HOST", self.default_host),
            "trade_events": os.getenv("MQ_TRADE_EVENTS_HOST", self.default_host),
            "market_state": os.getenv("MQ_MARKET_STATE_HOST", self.default_host),
            "system_cmd": os.getenv("MQ_SYSTEM_CMD_HOST", self.default_host),
            "logging": os.getenv("MQ_LOGGING_HOST", self.default_host),
            "reconciler": os.getenv("MQ_RECONCILER_HOST", self.default_host),
            "system_ctrl": os.getenv("MQ_SYSTEM_CTRL_HOST", self.default_host),
        }

    def _get_host(self, port, bind):
        if bind:
            return self.host # 0.0.0.0
        # Map ports to hosts for connections
        port_to_host = {
            Ports.MARKET_DATA: self.mq_hosts["market_data"],
            Ports.ORDERS: self.mq_hosts["orders"],
            Ports.TRADE_EVENTS: self.mq_hosts["trade_events"],
            Ports.MARKET_STATE: self.mq_hosts["market_state"],
            Ports.SYSTEM_CMD: self.mq_hosts["system_cmd"],
            Ports.LOGGING: self.mq_hosts["logging"],
            Ports.RECONCILER: self.mq_hosts["reconciler"],
            Ports.SYSTEM_CTRL: self.mq_hosts["system_ctrl"],
        }
        return port_to_host.get(port, "127.0.0.1")

    def create_publisher(self, port: int, bind: bool = True):
        """Creates an async Publisher socket."""
        socket = self.context.socket(zmq.PUB)
        host_str = self._get_host(port, bind)
        hosts = [h.strip() for h in host_str.split(",")]
        
        for host in hosts:
            addr = f"tcp://{host}:{port}"
            if bind:
                socket.bind(addr)
                self.logger.info(f"Publisher bound to {addr}")
            else:
                socket.connect(addr)
                self.logger.info(f"Publisher connected to {addr}")
        socket.setsockopt(zmq.LINGER, 1000)  # [F2-02] Prevent shutdown hang
        return socket

    def create_subscriber(self, port: int, topics: list | None = None, bind: bool = False):
        """Creates an async Subscriber socket."""
        if topics is None:
            topics = [""]
        socket = self.context.socket(zmq.SUB)
        host_str = self._get_host(port, bind)
        hosts = [h.strip() for h in host_str.split(",")]
        
        for host in hosts:
            addr = f"tcp://{host}:{port}"
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
        socket.setsockopt(zmq.RCVTIMEO, 5000)
        socket.setsockopt(zmq.LINGER, 1000)  # [F2-02]
        return socket

    def create_push(self, port: int, bind: bool = True):
        """Creates an async Push socket."""
        socket = self.context.socket(zmq.PUSH)
        host_str = self._get_host(port, bind)
        hosts = [h.strip() for h in host_str.split(",")]
        
        for host in hosts:
            addr = f"tcp://{host}:{port}"
            if bind:
                socket.bind(addr)
                self.logger.info(f"Push socket bound to {addr}")
            else:
                socket.connect(addr)
                self.logger.info(f"Push socket connected to {addr}")
        socket.setsockopt(zmq.LINGER, 1000)  # [F2-02] [F2-03] Removed RCVTIMEO (PUSH never receives)
        return socket

    def create_pull(self, port: int, bind: bool = False):
        """Creates an async Pull socket."""
        socket = self.context.socket(zmq.PULL)
        host_str = self._get_host(port, bind)
        hosts = [h.strip() for h in host_str.split(",")]
        
        for host in hosts:
            addr = f"tcp://{host}:{port}"
            if bind:
                socket.bind(addr)
                self.logger.info(f"Pull socket bound to {addr}")
            else:
                socket.connect(addr)
                self.logger.info(f"Pull socket connected to {addr}")
        socket.setsockopt(zmq.RCVTIMEO, 5000)
        socket.setsockopt(zmq.LINGER, 1000)  # [F2-02]
        return socket

    def create_dealer(self, port: int, identity: bytes | None = None, bind: bool = False):
        """Creates an async DEALER socket (pairs with ROUTER for decoupled async IPC)."""
        socket = self.context.socket(zmq.DEALER)
        if identity:
            socket.identity = identity
        host = self._get_host(port, bind)
        addr = f"tcp://{host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Dealer socket bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Dealer socket connected to {addr}")
        socket.setsockopt(zmq.RCVTIMEO, 5000)
        socket.setsockopt(zmq.LINGER, 1000)  # [F2-02]
        return socket

    def create_router(self, port: int, bind: bool = True):
        """Creates an async ROUTER socket (accepts DEALER connections for decoupled dispatch)."""
        socket = self.context.socket(zmq.ROUTER)
        host = self._get_host(port, bind)
        addr = f"tcp://{host}:{port}"
        if bind:
            socket.bind(addr)
            self.logger.info(f"Router socket bound to {addr}")
        else:
            socket.connect(addr)
            self.logger.info(f"Router socket connected to {addr}")
        socket.setsockopt(zmq.RCVTIMEO, 5000)
        socket.setsockopt(zmq.LINGER, 1000)  # [F2-02]
        return socket

    async def send_dealer(self, socket, data: dict, topic: str = None):
        """Send via DEALER socket (prepends empty delimiter frame for ROUTER compat)."""
        payload = json.dumps(data, cls=NumpyEncoder)
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

    async def send_json(self, socket, topic, data):
        """Sends data as a multipart message with correlation ID."""
        correlation_id = correlation_id_ctx.get() or str(uuid.uuid4())
        header = {"correlation_id": correlation_id}
        await socket.send_multipart([
            topic.encode('utf-8'),
            json.dumps(header).encode('utf-8'),
            json.dumps(data, cls=NumpyEncoder).encode('utf-8')
        ])

    async def recv_json(self, socket):
        """Receives data and extracts correlation ID."""
        multipart = await socket.recv_multipart()
        if len(multipart) < 3:
            raise ValueError(f"Malformed message: expected 3 parts, got {len(multipart)}")
            
        topic = multipart[0].decode('utf-8')
        header = json.loads(multipart[1].decode('utf-8'))
        data = json.loads(multipart[2].decode('utf-8'))
        
        correlation_id = header.get("correlation_id")
        if correlation_id:
            correlation_id_ctx.set(correlation_id)
            
        return topic, data

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
    HEDGE_REQUEST = 5563  # Strategy Engine requests hedge validation

class Topics:
    TICK_DATA = "TICK"
    MARKET_STATE = "STATE"
    SYSTEM_CMD = "CMD"
    ORDER_INTENT = "INTENT"
    FILL_RECEIPT = "FILL"
    HEDGE_REQUEST = "HEDGE"

class RedisLogger:
    """Streams logs to Redis for dashboard visibility."""
    def __init__(self, redis_url=None, key="live_logs", max_len=100):
        if redis_url is None:
            host = os.getenv("REDIS_HOST", "localhost")
            redis_pass = os.getenv("REDIS_PASSWORD", "")
            auth_str = f":{redis_pass}@" if redis_pass else ""
            redis_url = f"redis://{auth_str}{host}:6379"
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
