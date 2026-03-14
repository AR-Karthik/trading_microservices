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
    net_delta_sensex: float = 0.0
    veto: bool = False
    hw_alpha: list[float] = None  # To be initialized as [0.0]*10

class ShmManager:
    """
    Zero-latency Shared Memory IPC for Alpha Scores & Quantitative Signals.
    Layout: [Timestamp] [Alpha] [VPIN] [OFI] [Vanna] [Charm] [ENV] [STR] [DIV] [RV] [ADX] [PCR] [ND_N] [ND_B] [Veto] [CRC]
    """
    SIZE = 512 # Expanded for 10 heavyweights
    # Layout: [Timestamp] [Total Alpha] [VPIN] [OFI] [Vanna] [Charm] [ENV] [STR] [DIV] [RV] [ADX] [PCR] 
    #         [ND_Nifty] [ND_BNifty] [ND_Sensex] [Veto] [10x HW_Alpha] [CRC]
    # Format: d (ts) + 14d (base signals) + ? (veto) + 10d (hw_alpha) + d (crc)
    STRUCT_FORMAT = "ddddddddddddddd?ddddddddddd" 

    def __init__(self, asset_id: str = "GLOBAL", mode='r'):
        self.mode = mode
        self.asset_id = asset_id
        
        base_name = f"trading_alpha_{asset_id}"
        if os.name != 'nt':
            self.shm_path = f"/dev/shm/{base_name}"
        else:
            self.shm_path = base_name

        try:
            if os.name == 'nt':
                self.shm = mmap.mmap(-1, self.SIZE, tagname=self.shm_path)
            else:
                fd = os.open(self.shm_path, os.O_RDWR | os.O_CREAT, 0o666)
                os.ftruncate(fd, self.SIZE)
                self.shm = mmap.mmap(fd, self.SIZE)
        except Exception as e:
            logger.warning(f"Shared Memory [{asset_id}] initialization failed: {e}. Falling back to Redis.")
            self.shm = None

    def write(self, signals: SignalVector):
        """Standardized write using SignalVector dataclass."""
        if not self.shm: return
        try:
            ts = time.time()
            hw_a = signals.hw_alpha if signals.hw_alpha and len(signals.hw_alpha) == 10 else [0.0]*10
            
            # Cyclic check: sum of all signals
            crc = (signals.s_total + signals.vpin + signals.ofi_z + signals.vanna + 
                   signals.charm + signals.s_env + signals.s_str + signals.s_div + 
                   signals.rv + signals.adx + signals.pcr + 
                   signals.net_delta_nifty + signals.net_delta_banknifty + signals.net_delta_sensex +
                   sum(hw_a) + (100.0 if signals.veto else 0.0))
            
            data = struct.pack(
                self.STRUCT_FORMAT, 
                ts, signals.s_total, signals.vpin, signals.ofi_z, 
                signals.vanna, signals.charm, signals.s_env, signals.s_str, 
                signals.s_div, signals.rv, signals.adx, signals.pcr,
                signals.net_delta_nifty, signals.net_delta_banknifty, signals.net_delta_sensex,
                signals.veto, *hw_a, crc
            )
            self.shm.seek(0)
            self.shm.write(data)
        except Exception as e:
            logger.error(f"SHM write error: {e}")

    def read(self) -> dict | None:
        """Standardized read with integrity check."""
        if not self.shm: return None
        try:
            self.shm.seek(0)
            fmt_size = struct.calcsize(self.STRUCT_FORMAT)
            data = self.shm.read(fmt_size)
            
            read_data = struct.unpack(self.STRUCT_FORMAT, data)
            ts = read_data[0]
            s_total, vpin, ofi_z, vanna, charm, s_env, s_str, s_div = read_data[1:9]
            rv, adx, pcr, nd_nifty, nd_bnifty, nd_sensex = read_data[9:15]
            veto = read_data[15]
            hw_alphas = list(read_data[16:26])
            crc = read_data[26]
            
            # Check staleness (if > 1s, data is stale)
            if time.time() - ts > 1.0:
                return None
            
            # Integrity check
            check_val = (s_total + vpin + ofi_z + vanna + charm + s_env + s_str + s_div + 
                         rv + adx + pcr + nd_nifty + nd_bnifty + nd_sensex + sum(hw_alphas) + (100.0 if veto else 0.0))
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
                "net_delta_sensex": nd_sensex,
                "toxic_veto": veto,
                "hw_alpha": hw_alphas,
                "timestamp": ts,
                "source": "SHM"
            }
        except Exception as e:
            logger.error(f"SHM read error: {e}")
            return None
