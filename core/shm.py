import mmap
import struct
import time
import json
import os
import logging

logger = logging.getLogger("SHM")

class ShmManager:
    """
    Zero-latency Shared Memory IPC for Alpha Scores.
    Layout: [Timestamp (8b)] [Alpha (8b)] [VPIN (8b)] [OFI (8b)] [CRC (4b)]
    """
    SHM_NAME = "/dev/shm/trading_alpha" if os.name != 'nt' else "trading_alpha"
    SIZE = 64  # Reserve 64 bytes
    STRUCT_FORMAT = "ddddd" # ts, alpha, vpin, ofi, crc

    def __init__(self, mode='r'):
        self.mode = mode
        try:
            if os.name == 'nt':
                # Windows uses named memory mapping
                self.shm = mmap.mmap(-1, self.SIZE, tagname=self.SHM_NAME)
            else:
                # Linux uses /dev/shm or shm_open
                fd = os.open(self.SHM_NAME, os.O_RDWR | os.O_CREAT, 0o666)
                os.ftruncate(fd, self.SIZE)
                self.shm = mmap.mmap(fd, self.SIZE)
        except Exception as e:
            logger.warning(f"Shared Memory initialization failed: {e}. Falling back to Redis.")
            self.shm = None

    def write(self, s_total: float, vpin: float, ofi_z: float):
        if not self.shm: return
        try:
            ts = time.time()
            # Simple CRC or sum for integrity check
            crc = s_total + vpin + ofi_z
            data = struct.pack(self.STRUCT_FORMAT, ts, s_total, vpin, ofi_z, crc)
            self.shm.seek(0)
            self.shm.write(data)
        except Exception as e:
            logger.error(f"SHM write error: {e}")

    def read(self) -> dict | None:
        if not self.shm: return None
        try:
            self.shm.seek(0)
            data = self.shm.read(struct.calcsize(self.STRUCT_FORMAT))
            ts, s_total, vpin, ofi_z, crc = struct.unpack(self.STRUCT_FORMAT, data)
            
            # Check staleness (if > 500ms, data is considered stale)
            if time.time() - ts > 0.5:
                return None
            
            # Basic integrity check
            if abs(crc - (s_total + vpin + ofi_z)) > 1e-6:
                return None
                
            return {
                "s_total": s_total,
                "vpin": vpin,
                "ofi_zscore": ofi_z,
                "timestamp": ts,
                "source": "SHM"
            }
        except Exception:
            return None
