import zmq
import zmq.asyncio
import json
import logging
import os
import uuid
import contextvars

from .encoders import NumpyEncoder

correlation_id_ctx = contextvars.ContextVar("correlation_id", default=None)

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
    RAW_INTENTS = 5564    # Strategy Engine pushes RAW intents to Router

class Topics:
    TICK_DATA = "TICK"
    MARKET_STATE = "STATE"
    SYSTEM_CMD = "CMD"
    ORDER_INTENT = "INTENT"
    FILL_RECEIPT = "FILL"
    HEDGE_REQUEST = "HEDGE"
    RAW_INTENT = "RAW_INTENT"

class MQManager:
    """Wrapper around ZeroMQ for async pub/sub and push/pull messaging."""
    
    # NO LOSS OF EXISTING FUNCTIONALITY, NO ACCIDENTAL DELETIONS, NO OVERSIGHT - GCP CONTAINER SAFE
    def __init__(self, hosts: dict | None = None):
        self.context = zmq.asyncio.Context()
        self.logger = logging.getLogger("MQ")
        self.host = "0.0.0.0"
        default_host = os.getenv("MQ_DEFAULT_HOST", "127.0.0.1")
        self.mq_hosts = hosts or {
            "market_data": os.getenv("MQ_MARKET_DATA_HOST", default_host),
            "orders": os.getenv("MQ_ORDERS_HOST", default_host),
            "trade_events": os.getenv("MQ_TRADE_EVENTS_HOST", default_host),
            "market_state": os.getenv("MQ_MARKET_STATE_HOST", default_host),
            "system_cmd": os.getenv("MQ_SYSTEM_CMD_HOST", default_host),
            "logging": os.getenv("MQ_LOGGING_HOST", default_host),
            "reconciler": os.getenv("MQ_RECONCILER_HOST", default_host),
            "system_ctrl": os.getenv("MQ_SYSTEM_CTRL_HOST", default_host),
        }

    def _get_host(self, port, bind):
        if bind:
            return self.host # 0.0.0.0
        # Map ports to hosts for connections
        port_to_host = {
            Ports.MARKET_DATA: self.mq_hosts.get("market_data", "127.0.0.1"),
            Ports.ORDERS: self.mq_hosts.get("orders", "127.0.0.1"),
            Ports.TRADE_EVENTS: self.mq_hosts.get("trade_events", "127.0.0.1"),
            Ports.MARKET_STATE: self.mq_hosts.get("market_state", "127.0.0.1"),
            Ports.SYSTEM_CMD: self.mq_hosts.get("system_cmd", "127.0.0.1"),
            Ports.LOGGING: self.mq_hosts.get("logging", "127.0.0.1"),
            Ports.RECONCILER: self.mq_hosts.get("reconciler", "127.0.0.1"),
            Ports.SYSTEM_CTRL: self.mq_hosts.get("system_ctrl", "127.0.0.1"),
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
