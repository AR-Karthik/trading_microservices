import struct
import time
from multiprocessing import shared_memory
import logging

logger = logging.getLogger("SharedMemory")

# 32s: Symbol, d: Price, q: Volume, d: Timestamp, d: Latency, Q: Hires, Q: Seq, d: High, d: Low
TICK_STRUCT_FORMAT = "32s d q d d Q Q d d"
TICK_STRUCT_SIZE = struct.calcsize(TICK_STRUCT_FORMAT)
MAX_SLOTS = 1000
SHM_NAME = "trading_ticks_shm"
SHM_SIZE = TICK_STRUCT_SIZE * MAX_SLOTS

# Deterministic Slot Mapping (Source of Truth)
# These are the "Fast Lane" instruments that have dedicated SHM slots.
# Dynamic symbols (options JIT) are published via ZMQ/Redis but don't occupy SHM slots 
# unless they are part of the core monitored universe.
CORE_SYMBOLS = [
    "NIFTY50", "BANKNIFTY", "SENSEX",
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "ITC", "SBIN", "AXISBANK", "KOTAKBANK", "LT",
    "INDIAVIX"
]
SYMBOL_TO_SLOT = {s: i for i, s in enumerate(CORE_SYMBOLS)}
SLOT_TO_SYMBOL = {i: s for s, i in SYMBOL_TO_SLOT.items()}

class TickSharedMemory:
    def __init__(self, create: bool = False):
        self.shm: shared_memory.SharedMemory | None = None
        try:
            if create:
                # Try to clean up if it already exists
                try:
                    old_shm = shared_memory.SharedMemory(name=SHM_NAME)
                    old_shm.close()
                    old_shm.unlink()
                except Exception:
                    pass
                self.shm = shared_memory.SharedMemory(name=SHM_NAME, create=True, size=SHM_SIZE)
            else:
                self.shm = shared_memory.SharedMemory(name=SHM_NAME)
        except Exception as e:
            logger.error(f"Failed to initialize Shared Memory: {e}")
            raise

    def write_tick(self, slot_index, symbol, price, volume, timestamp=None, latency_ms=0.0, hires_ts=None, sequence_id=0, high=0.0, low=0.0):
        if not (0 <= slot_index < MAX_SLOTS):
            raise ValueError("Slot index out of range")
        
        if self.shm is None:
            logger.error("Shared memory not initialized")
            return
            
        # Ensure symbol is bytes and padded to 32 bytes
        symbol_bytes = symbol.encode('utf-8')[:31].ljust(32, b'\0')
        
        # [Audit-Fix] Component 4: High-Resolution Heartbeat (Sequence/Epoch)
        effective_ts = timestamp if timestamp is not None else time.time()
        # hires_ts for staleness check (e.g., microsecond epoch as uint64)
        effective_hires = hires_ts if hires_ts is not None else int(time.time() * 1_000_000)

        packed_data = struct.pack(TICK_STRUCT_FORMAT, symbol_bytes, price, volume, effective_ts, float(latency_ms), effective_hires, sequence_id, float(high), float(low))
        offset = slot_index * TICK_STRUCT_SIZE
        # Using a slice object to help the linter understand the mapping
        self.shm.buf[offset : offset + TICK_STRUCT_SIZE] = packed_data

    def read_tick(self, slot_index):
        if self.shm is None:
            return None
            
        offset = slot_index * TICK_STRUCT_SIZE
        data = self.shm.buf[offset : offset + TICK_STRUCT_SIZE]
        
        symbol_bytes, price, volume, timestamp, latency_ms, hires_ts, sequence_id, high, low = struct.unpack(TICK_STRUCT_FORMAT, data)
        symbol = symbol_bytes.decode('utf-8').strip('\0')
        
        return {
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "timestamp": timestamp,
            "latency_ms": latency_ms,
            "hires_ts": hires_ts,
            "sequence_id": sequence_id,
            "day_high": high,
            "day_low": low
        }

    def close(self):
        if self.shm:
            self.shm.close()

    def unlink(self):
        if self.shm:
            try:
                self.shm.unlink()
            except Exception:
                pass
