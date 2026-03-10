import mmap
import struct
import time
import json
import os
import logging

logger = logging.getLogger("SHM")

class ShmManager:
    """
    Zero-latency Shared Memory IPC for Alpha Scores & Quantitative Signals (v5.5).
    Layout: [Timestamp] [Alpha] [VPIN] [OFI] [Vanna] [Charm] [ENV] [STR] [DIV] [Veto] [CRC]
    """
    SHM_NAME = "/dev/shm/trading_alpha" if os.name != 'nt' else "trading_alpha"
    SIZE = 128  # Increased for v5.5
    STRUCT_FORMAT = "ddddddddd?d" # ts, alpha, vpin, ofi, vanna, charm, s_env, s_str, s_div, veto, crc

    def __init__(self, mode='r'):
        self.mode = mode
        try:
            if os.name == 'nt':
                self.shm = mmap.mmap(-1, self.SIZE, tagname=self.SHM_NAME)
            else:
                fd = os.open(self.SHM_NAME, os.O_RDWR | os.O_CREAT, 0o666)
                os.ftruncate(fd, self.SIZE)
                self.shm = mmap.mmap(fd, self.SIZE)
        except Exception as e:
            logger.warning(f"Shared Memory initialization failed: {e}. Falling back to Redis.")
            self.shm = None

    def write(self, s_total, vpin, ofi_z, vanna=0.0, charm=0.0, s_env=0.0, s_str=0.0, s_div=0.0, veto=False):
        if not self.shm: return
        try:
            ts = time.time()
            # Cyclic check: sum of all signals
            crc = s_total + vpin + ofi_z + vanna + charm + s_env + s_str + s_div + (100.0 if veto else 0.0)
            data = struct.pack(self.STRUCT_FORMAT, ts, s_total, vpin, ofi_z, vanna, charm, s_env, s_str, s_div, veto, crc)
            self.shm.seek(0)
            self.shm.write(data)
        except Exception as e:
            logger.error(f"SHM write error: {e}")

    def read(self) -> dict | None:
        if not self.shm: return None
        try:
            self.shm.seek(0)
            fmt_size = struct.calcsize(self.STRUCT_FORMAT)
            data = self.shm.read(fmt_size)
            ts, s_total, vpin, ofi_z, vanna, charm, s_env, s_str, s_div, veto, crc = struct.unpack(self.STRUCT_FORMAT, data)
            
            # Check staleness (if > 1s, data is stale - loosened from 0.5s for v5.5)
            if time.time() - ts > 1.0:
                return None
            
            # Integrity check
            check_val = s_total + vpin + ofi_z + vanna + charm + s_env + s_str + s_div + (100.0 if veto else 0.0)
            if abs(crc - check_val) > 1e-4:
                return None
                
            return {
                "s_total": s_total,
                "vpin": vpin,
                "ofi_zscore": ofi_z,
                "vanna": vanna,
                "charm": charm,
                "s_env": s_env,
                "s_str": s_str,
                "s_div": s_div,
                "toxic_veto": veto,
                "timestamp": ts,
                "source": "SHM"
            }
        except Exception:
            return None
