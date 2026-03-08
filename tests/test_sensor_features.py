import polars as pl
import numpy as np
import pytest
from daemons.market_sensor import MarketSensor

def test_hurst_calc():
    sensor = MarketSensor(test_mode=True)
    # Trending series (Geometric Brownian Motion with drift)
    trending = np.cumsum(np.random.normal(0.01, 0.05, 1000) + 100)
    h_trend = sensor.calculate_hurst(trending)
    
    # Random walk (roughly 0.5)
    random_walk = np.cumsum(np.random.randn(1000))
    h_random = sensor.calculate_hurst(random_walk)
    
    assert h_trend > 0.4 # Trending usually > 0.5 but with noise > 0.4
    assert 0.3 < h_random < 0.7

def test_realized_vol():
    sensor = MarketSensor(test_mode=True)
    # High vol series
    prices_high = np.random.normal(100, 2.0, 100)
    rv_high = sensor.calculate_rv(prices_high)
    
    # Low vol series
    prices_low = np.random.normal(100, 0.1, 100)
    rv_low = sensor.calculate_rv(prices_low)
    
    assert rv_high > rv_low

def test_ofi_calculation():
    sensor = MarketSensor(test_mode=True)
    # Fake book data: Aggressive buying (Price up, Volume up)
    ticks = [
        {"price": 100, "volume": 10, "type": "TICK"},
        {"price": 101, "volume": 20, "type": "TICK"},
        {"price": 102, "volume": 30, "type": "TICK"}
    ]
    df = pl.DataFrame(ticks)
    ofi = sensor.calculate_ofi(df)
    assert ofi > 0 # Buying pressure
