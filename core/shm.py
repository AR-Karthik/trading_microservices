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
    asto: float = 0.0
    asto_regime: float = 0.0
    whale_pivot: float = 0.0
    net_delta_nifty: float = 0.0
    net_delta_banknifty: float = 0.0
    net_delta_sensex: float = 0.0
    # [New Vector Fields]
    high_z: float = 0.0
    low_z: float = 0.0
    iv_percentile: float = 0.0
    iv_rv_spread: float = 0.0
    smart_flow: float = 0.0
    buy_p: float = 0.0
    sell_p: float = 0.0
    pcr_roc: float = 0.0
    hb_ts: float = 0.0
    veto: bool = False
    raw_veto: bool = False
    stale_flag: bool = False
    hw_alpha: list[float] = None

@dataclass
class RegimeVector:
    s18_int: int         # 0: Neutral, 1: Trending, 2: Ranging, 3: Volatile
    s26_persistence: float # 0-100%
    s27_quality: float     # 0-100%
    veto: bool
    timestamp: float

class ShmManager:
    """
    Handles memory-mapped inter-process communication for transmitting high-frequency quantitative signals.
    """
    # Pre-allocate 1KB memory map buffer size for current and future structural scaling
    SIZE = 1024 
    # Data packing format matching SignalVector attributes
    # 27 doubles, 3 booleans, 10 doubles (hw), 1 double (crc)
    STRUCT_FORMAT = "ddddddddddddddddddddddddddd???ddddddddddd" 

    def __init__(self, asset_id: str = "GLOBAL", mode='r'):
        self.mode = mode
        self.asset_id = asset_id
        
        base_name = f"trading_alpha_{asset_id}"
        if os.name != 'nt':
            # Use /ram_disk for inter-container shared memory if available
            if os.path.exists("/ram_disk"):
                self.shm_path = f"/ram_disk/{base_name}"
            else:
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
        """Serializes and writes the SignalVector state, including checksum, into shared memory."""
        if not self.shm: return
        try:
            ts = time.time()
            hw_a = signals.hw_alpha if signals.hw_alpha and len(signals.hw_alpha) == 10 else [0.0]*10
            
            # Calculate primitive checksum for data integrity verification
            crc = (signals.s_total + signals.vpin + signals.ofi_z + signals.vanna + 
                   signals.charm + signals.s_env + signals.s_str + signals.s_div + 
                   signals.rv + signals.adx + signals.pcr + signals.asto + signals.asto_regime +
                   signals.whale_pivot +
                   signals.net_delta_nifty + signals.net_delta_banknifty + signals.net_delta_sensex +
                   signals.high_z + signals.low_z + signals.iv_percentile + signals.iv_rv_spread +
                   signals.smart_flow + signals.buy_p + signals.sell_p + signals.pcr_roc + signals.hb_ts +
                   sum(hw_a) + 
                   (100.0 if signals.veto else 0.0) + (100.0 if signals.raw_veto else 0.0) + (100.0 if signals.stale_flag else 0.0))
            
            data = struct.pack(
                self.STRUCT_FORMAT, 
                ts, signals.s_total, signals.vpin, signals.ofi_z, 
                signals.vanna, signals.charm, signals.s_env, signals.s_str, 
                signals.s_div, signals.rv, signals.adx, signals.pcr,
                signals.asto, signals.asto_regime, signals.whale_pivot,
                signals.net_delta_nifty, signals.net_delta_banknifty, signals.net_delta_sensex,
                signals.high_z, signals.low_z, signals.iv_percentile, signals.iv_rv_spread,
                signals.smart_flow, signals.buy_p, signals.sell_p, signals.pcr_roc, signals.hb_ts,
                signals.veto, signals.raw_veto, signals.stale_flag,
                *hw_a, crc
            )
            self.shm.seek(0)
            self.shm.write(data)
        except Exception as e:
            logger.error(f"SHM write error: {e}")

    def read(self) -> dict | None:
        """Reads, deserializes, and validates the shared memory buffer into a signal dictionary."""
        if not self.shm: return None
        try:
            self.shm.seek(0)
            fmt_size = struct.calcsize(self.STRUCT_FORMAT)
            data = self.shm.read(fmt_size)
            
            read_data = struct.unpack(self.STRUCT_FORMAT, data)
            ts = read_data[0]
            # 1 to 18 (Existing Doubles)
            s_total, vpin, ofiz, vanna, charm, senv, sstr, sdiv = read_data[1:9]
            rv, adx, pcr, asto, asto_reg, wh_p, nd_nift, nd_bn, nd_sen = read_data[9:18]
            # 18 to 27 (New Doubles)
            hi_z, lo_z, iv_p, iv_rv, sf, bp, sp, p_roc, hb = read_data[18:27]
            # 27 to 30 (Booleans)
            veto, r_veto, s_flag = read_data[27:30]
            # 30 to 40 (HW Alphas)
            hw_alphas = list(read_data[30:40])
            # 40 (CRC)
            crc = read_data[40]
            
            # Reject payload if delta from write timestamp exceeds 1.0 second
            if time.time() - ts > 1.0:
                return None
            
            # Validate checksum sum against the stored CRC value to ensure atomic read
            check_val = (s_total + vpin + ofiz + vanna + charm + senv + sstr + sdiv + 
                         rv + adx + pcr + asto + asto_reg + wh_p + nd_nift + nd_bn + nd_sen +
                         hi_z + lo_z + iv_p + iv_rv + sf + bp + sp + p_roc + hb +
                         sum(hw_alphas) + 
                         (100.0 if veto else 0.0) + (100.0 if r_veto else 0.0) + (100.0 if s_flag else 0.0))
            
            if abs(crc - check_val) > 1e-4:
                return None
                
            return {
                "s_total": s_total, "vpin": vpin, "ofi_zscore": ofiz, "vanna": vanna, "charm": charm,
                "s_env": senv, "s_str": sstr, "s_div": sdiv, "rv": rv, "adx": adx, "pcr": pcr,
                "asto": asto, "asto_regime": asto_reg, "whale_pivot": wh_p,
                "net_delta_nifty": nd_nift, "net_delta_banknifty": nd_bn, "net_delta_sensex": nd_sen,
                "high_z": hi_z, "low_z": lo_z, "iv_percentile": iv_p, "iv_rv_spread": iv_rv,
                "smart_flow": sf, "buy_p": bp, "sell_p": sp, "pcr_roc": p_roc, "hb_ts": hb,
                "toxic_veto": veto, "raw_veto": r_veto, "stale_flag": s_flag,
                "hw_alpha": hw_alphas, "timestamp": ts, "source": "SHM"
            }
        except Exception as e:
            logger.error(f"SHM read error: {e}")
            return None

class RegimeShm:
    """
    Dedicated memory-mapped manager for HMM Regime Vectors.
    Enables ultra-low latency broadcasting of market state verdicts.
    """
    SIZE = 256
    STRUCT_FORMAT = "i d d ? d" # s18, persistence, quality, veto, ts

    def __init__(self, asset_id: str, mode='r'):
        self.mode = mode
        self.asset_id = asset_id
        self.shm_path = f"regime_alpha_{asset_id}"
        
        try:
            if os.name == 'nt':
                self.shm = mmap.mmap(-1, self.SIZE, tagname=self.shm_path)
            else:
                shm_file = f"/dev/shm/{self.shm_path}"
                fd = os.open(shm_file, os.O_RDWR | os.O_CREAT, 0o666)
                os.ftruncate(fd, self.SIZE)
                self.shm = mmap.mmap(fd, self.SIZE)
        except Exception as e:
            logger.warning(f"Regime SHM [{asset_id}] initialization failed: {e}")
            self.shm = None

    def write(self, vec: RegimeVector):
        if not self.shm: return
        try:
            data = struct.pack(self.STRUCT_FORMAT, vec.s18_int, vec.s26_persistence, vec.s27_quality, vec.veto, vec.timestamp)
            self.shm.seek(0)
            self.shm.write(data)
        except Exception as e:
            logger.error(f"Regime SHM write error: {e}")

    def read(self) -> dict | None:
        if not self.shm: return None
        try:
            self.shm.seek(0)
            data = self.shm.read(struct.calcsize(self.STRUCT_FORMAT))
            s18, s26, s27, veto, ts = struct.unpack(self.STRUCT_FORMAT, data)
            return {
                "regime_s18": s18,
                "persistence_s26": s26,
                "quality_s27": s27,
                "veto": veto,
                "timestamp": ts,
                "source": "REGIME_SHM"
            }
        except Exception as e:
            logger.error(f"Regime SHM read error: {e}")
            return None
