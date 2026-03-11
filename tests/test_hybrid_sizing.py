"""
tests/test_hybrid_sizing.py
============================
Unit tests for v8.0 Hybrid ATR×Kelly Sizing Logic
  - Feature A: ATR Unit = MAX_RISK_PER_TRADE / (ATR × ATR_SL_MULTIPLIER)
  - Feature B: Kelly Multiplier = max(0.01, 0.5 × kelly_f)
  - Combined: final_lots = unit_size × half_kelly
"""
import math
import unittest

# ── Constants (mirror meta_router.py) ──────────────────────────────────────────
MAX_RISK_PER_TRADE = 2500.0
ATR_SL_MULTIPLIER  = 1.0


def compute_hybrid_lots(atr: float, p: float) -> dict:
    """
    Inline replica of BaseStrategyLogic.calculate_weights() hybrid sizing.
    Used for isolated unit testing without dependency on Redis or ZMQ.
    """
    atr = max(atr, 1.0)  # Guard
    unit_size = MAX_RISK_PER_TRADE / (atr * ATR_SL_MULTIPLIER)

    b = 1.5
    kelly_f = p - ((1.0 - p) / b)
    half_kelly = max(0.01, 0.5 * kelly_f)

    final_lots = round(unit_size * half_kelly, 4)

    return {
        "unit_size": round(unit_size, 4),
        "half_kelly": round(half_kelly, 4),
        "final_lots": final_lots,
        "atr": atr
    }


class TestATRUnit(unittest.TestCase):
    """Step A: ATR Unit floor — normalizes rupee risk at stop-loss."""

    def test_standard_atr(self):
        """ATR=20 → unit_size = ₹2500 / 20 = 125 lots."""
        r = compute_hybrid_lots(atr=20.0, p=0.6)
        self.assertAlmostEqual(r["unit_size"], 125.0, places=2)

    def test_high_atr_reduces_size(self):
        """ATR=50 → unit_size = ₹2500 / 50 = 50 lots."""
        r = compute_hybrid_lots(atr=50.0, p=0.6)
        self.assertAlmostEqual(r["unit_size"], 50.0, places=2)

    def test_low_atr_increases_size(self):
        """ATR=10 → unit_size = ₹2500 / 10 = 250 lots."""
        r = compute_hybrid_lots(atr=10.0, p=0.6)
        self.assertAlmostEqual(r["unit_size"], 250.0, places=2)

    def test_zero_atr_clamped_to_one(self):
        """ATR=0 must NOT cause ZeroDivisionError — clamped to 1.0."""
        r = compute_hybrid_lots(atr=0.0, p=0.6)
        self.assertAlmostEqual(r["unit_size"], 2500.0, places=2)

    def test_unit_size_inversely_proportional_to_atr(self):
        """Doubling ATR must halve unit_size."""
        r1 = compute_hybrid_lots(atr=20.0, p=0.6)
        r2 = compute_hybrid_lots(atr=40.0, p=0.6)
        self.assertAlmostEqual(r1["unit_size"], r2["unit_size"] * 2, places=2)


class TestKellyMultiplier(unittest.TestCase):
    """Step B: Half-Kelly fraction from conviction probability."""

    def test_high_probability_high_kelly(self):
        """p=0.9 → kelly_f=0.9-(0.1/1.5)≈0.833, half_kelly≈0.417."""
        r = compute_hybrid_lots(atr=20.0, p=0.9)
        expected_kf = 0.9 - (0.1 / 1.5)
        expected_hk = max(0.01, 0.5 * expected_kf)
        self.assertAlmostEqual(r["half_kelly"], expected_hk, places=4)

    def test_low_probability_clamps_to_floor(self):
        """p=0.3 → kelly_f is negative → half_kelly clamped to 0.01."""
        r = compute_hybrid_lots(atr=20.0, p=0.3)
        self.assertAlmostEqual(r["half_kelly"], 0.01, places=4)

    def test_neutral_probability(self):
        """p=0.6 → kelly_f=0.6-(0.4/1.5)≈0.333, half_kelly≈0.167."""
        r = compute_hybrid_lots(atr=20.0, p=0.6)
        expected_kf = 0.6 - (0.4 / 1.5)
        expected_hk = max(0.01, 0.5 * expected_kf)
        self.assertAlmostEqual(r["half_kelly"], expected_hk, places=4)

    def test_breakeven_probability(self):
        """p=0.5 → kelly_f = 0.5 - (0.5/1.5) = 0.5 - 0.333 = 0.167, half_kelly≈0.083."""
        r = compute_hybrid_lots(atr=20.0, p=0.5)
        expected_kf = 0.5 - (0.5 / 1.5)
        expected_hk = max(0.01, 0.5 * expected_kf)
        self.assertAlmostEqual(r["half_kelly"], expected_hk, places=4)


class TestCombinedHybridSizing(unittest.TestCase):
    """Final: final_lots = unit_size × half_kelly — integration of A and B."""

    def test_final_lots_formula(self):
        """ATR=20, p=0.6 → lots = 125 × half_kelly (pre-rounding), rounded to 4dp."""
        r = compute_hybrid_lots(atr=20.0, p=0.6)
        # Recompute from raw (unrounded) inputs to avoid double-rounding mismatch
        raw_unit = MAX_RISK_PER_TRADE / (20.0 * ATR_SL_MULTIPLIER)    # 125.0
        raw_kf   = 0.6 - (0.4 / 1.5)
        raw_hk   = max(0.01, 0.5 * raw_kf)
        expected = round(raw_unit * raw_hk, 4)
        self.assertAlmostEqual(r["final_lots"], expected, places=4)
        self.assertGreater(r["final_lots"], 0)

    def test_high_atr_high_prob_moderate_lots(self):
        """ATR=50, p=0.9 → unit=50, kelly≈0.417 → lots≈20.83."""
        r = compute_hybrid_lots(atr=50.0, p=0.9)
        self.assertGreater(r["final_lots"], 0)
        self.assertLess(r["final_lots"], 100)

    def test_low_atr_low_prob_small_lots(self):
        """ATR=10, p=0.35 → kelly clamped to 0.01 → very few lots."""
        r = compute_hybrid_lots(atr=10.0, p=0.35)
        self.assertEqual(r["half_kelly"], 0.01)
        self.assertAlmostEqual(r["final_lots"], 250.0 * 0.01, places=2)

    def test_rupee_loss_at_stop_is_bounded(self):
        """
        When price hits SL (1×ATR below entry), rupee loss must always be:
          rupee_loss = final_lots × ATR × ATR_SL_MULTIPLIER ≤ MAX_RISK_PER_TRADE
        (inequality because half_kelly ≤ 1)
        """
        for atr, p in [(20, 0.6), (50, 0.9), (10, 0.5), (100, 0.8)]:
            r = compute_hybrid_lots(atr=atr, p=p)
            rupee_loss = r["final_lots"] * r["atr"] * ATR_SL_MULTIPLIER
            self.assertLessEqual(rupee_loss, MAX_RISK_PER_TRADE + 1.0,
                msg=f"ATR={atr}, p={p}: rupee_loss={rupee_loss:.2f} > ₹{MAX_RISK_PER_TRADE}")


if __name__ == "__main__":
    unittest.main()
