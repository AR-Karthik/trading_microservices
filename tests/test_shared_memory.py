import pytest
import numpy as np
import time
from core.shared_memory import TickSharedMemory

def test_shared_memory_read_write():
    """
    Validates that ticks written to shared memory can be read back accurately.
    """
    tsm = TickSharedMemory(create=True)
    
    try:
        # Write some ticks
        ticks = [
            {"slot": 0, "symbol": "NIFTY", "price": 22000.5, "v": 100},
            {"slot": 1, "symbol": "BANKNIFTY", "price": 48000.0, "v": 50},
        ]
        
        for tick in ticks:
            tsm.write_tick(tick["slot"], tick["symbol"], tick["price"], tick["v"], time.time())
            
        # Read back
        for tick in ticks:
            res = tsm.read_tick(tick["slot"])
            assert res["symbol"] == tick["symbol"]
            assert res["price"] == tick["price"]
            
        # Non-existent symbol via new slot
        res = tsm.read_tick(999)
        assert res["symbol"] == "" # Default empty from pack
        
    finally:
        tsm.close()
        tsm.unlink()

def test_shared_memory_persistence():
    """
    Validates that a new TSM instance can read from an existing shared memory block.
    """
    tsm_writer = TickSharedMemory(create=True)
    tsm_writer.write_tick(5, "RELIANCE", 2500.75, 10, time.time())
    
    # Open new reader instance
    tsm_reader = TickSharedMemory(create=False)
    res = tsm_reader.read_tick(5)
    assert res["symbol"] == "RELIANCE"
    assert res["price"] == 2500.75
    
    tsm_writer.close()
    tsm_reader.close()
    tsm_writer.unlink()
