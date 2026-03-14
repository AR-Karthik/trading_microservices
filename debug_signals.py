import sys
import os
import numpy as np

# Ensure we can import from the root
sys.path.append(os.getcwd())

from daemons.market_sensor import calculate_kaufman_er, find_zero_gamma_level

def debug_kaufman():
    print("Testing Kaufman ER...")
    # series needs window+1 points for Kaufman ER
    # series = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11] (len=11)
    # window = 10
    # series[-(10+1):] = series[0:11]
    # net_change = abs(11 - 1) = 10
    # diffs = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1] (sum=10)
    # result = 1.0
    series = np.arange(1, 12.0)
    er = calculate_kaufman_er(series, window=10)
    print(f"ER (Expected 1.0): {er}")
    assert er == pytest.approx(1.0) if 'pytest' in sys.modules else er == 1.0

    series_flat = np.ones(11)
    er_flat = calculate_kaufman_er(series_flat, window=10)
    print(f"ER Flat (Expected 0.0): {er_flat}")
    assert er_flat == 0.0, f"Expected 0.0, got {er_flat}"

def debug_zgl():
    print("Testing Zero Gamma Level...")
    prices = np.array([100, 101, 102, 103, 104])
    zgl = find_zero_gamma_level(prices, 104)
    # np.mean([100, 101, 102, 103, 104]) = 102.0
    print(f"ZGL (Expected 102.0): {zgl}")
    assert zgl == 102.0, f"Expected 102.0, got {zgl}"

if __name__ == "__main__":
    try:
        debug_kaufman()
        debug_zgl()
        print("Success!")
    except Exception as e:
        print(f"FAILURE: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
