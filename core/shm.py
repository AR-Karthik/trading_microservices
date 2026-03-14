import mmap
import struct
import time
import json
import os
import logging

logger = logging.getLogger("SHM")

from dataclasses import dataclass

@dataclass
class SignalVector:
    s_total: float
    vpin: float
    ofi_z: float
    vanna: float = 0.0
    charm: float = 0.0
    s_env: float = 0.0
    s_str: float = 0.0
    s_div: float = 0.0
    rv: float = 0.0
    adx: float = 0.0
    pcr: float = 0.0
    net_delta_nifty: float = 0.0
    net_delta_banknifty: float = 0.0
    veto: bool = False

class ShmManager:
    """
    Zero-latency Shared Memory IPC for Alpha Scores & Quantitative Signals (v6.0).
    Layout: [Timestamp] [Alpha] [VPIN] [OFI] [Vanna] [Charm] [ENV] [STR] [DIV] [RV] [ADX] [PCR] [ND_N] [ND_B] [Veto] [CRC]
    """
    SHM_NAME = "/dev/shm/trading_alpha" if os.name != 'nt' else "trading_alpha"
    SIZE = 256  # Increased for v6.0 resilience
    # Format: ts(d), s_total(d), vpin(d), ofi(d), vanna(d), charm(d), s_env(d), s_str(d), s_div(d), 
    #         rv(d), adx(d), pcr(d), nd_nifty(d), nd_bnifty(d), veto(?), crc(d)
    STRUCT_FORMAT = "ddddddddddddddd?d" 

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

    def write(self, signals: SignalVector):
        """Standardized write using SignalVector dataclass (#1971)"""
        if not self.shm: return
        try:
            ts = time.time()
            # Cyclic check: sum of all signals
            crc = (signals.s_total + signals.vpin + signals.ofi_z + signals.vanna + 
                   signals.charm + signals.s_env + signals.s_str + signals.s_div + 
                   signals.rv + signals.adx + signals.pcr + 
                   signals.net_delta_nifty + signals.net_delta_banknifty +
                   (100.0 if signals.veto else 0.0))
            
            data = struct.pack(
                self.STRUCT_FORMAT, 
                ts, signals.s_total, signals.vpin, signals.ofi_z, 
                signals.vanna, signals.charm, signals.s_env, signals.s_str, 
                signals.s_div, signals.rv, signals.adx, signals.pcr,
                signals.net_delta_nifty, signals.net_delta_banknifty,
                signals.veto, crc
            )
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
            (ts, s_total, vpin, ofi_z, vanna, charm, s_env, s_str, s_div, 
             rv, adx, pcr, nd_nifty, nd_bnifty, veto, crc) = struct.unpack(self.STRUCT_FORMAT, data)
            
            # Check staleness (if > 1s, data is stale)
            if time.time() - ts > 1.0:
                return None
            
            # Integrity check
            check_val = (s_total + vpin + ofi_z + vanna + charm + s_env + s_str + s_div + 
                         rv + adx + pcr + nd_nifty + nd_bnifty + (100.0 if veto else 0.0))
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
                "rv": rv,
                "adx": adx,
                "pcr": pcr,
                "net_delta_nifty": nd_nifty,
                "net_delta_banknifty": nd_bnifty,
                "toxic_veto": veto,
                "timestamp": ts,
                "source": "SHM"
            }
        except Exception:
            return None
