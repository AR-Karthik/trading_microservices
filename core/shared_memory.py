import struct
import time
from multiprocessing import shared_memory
import logging

logger = logging.getLogger("SharedMemory")

# Structure definition for a single tick
# 32s: Symbol (bytes), d: Price (float64), q: Volume (int64), d: Timestamp (float64), Q: Hires Heartbeat (uint64)
TICK_STRUCT_FORMAT = "32s d q d Q"
TICK_STRUCT_SIZE = struct.calcsize(TICK_STRUCT_FORMAT)
MAX_SLOTS = 1000
SHM_NAME = "trading_ticks_shm"
SHM_SIZE = TICK_STRUCT_SIZE * MAX_SLOTS

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

    def write_tick(self, slot_index, symbol, price, volume, timestamp=None, hires_ts=None):
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

        packed_data = struct.pack(TICK_STRUCT_FORMAT, symbol_bytes, price, volume, effective_ts, effective_hires)
        offset = slot_index * TICK_STRUCT_SIZE
        # Using a slice object to help the linter understand the mapping
        self.shm.buf[offset : offset + TICK_STRUCT_SIZE] = packed_data

    def read_tick(self, slot_index):
        if self.shm is None:
            return None
            
        offset = slot_index * TICK_STRUCT_SIZE
        data = self.shm.buf[offset : offset + TICK_STRUCT_SIZE]
        
        symbol_bytes, price, volume, timestamp, hires_ts = struct.unpack(TICK_STRUCT_FORMAT, data)
        symbol = symbol_bytes.decode('utf-8').strip('\0')
        
        return {
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "timestamp": timestamp,
            "hires_ts": hires_ts
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
