"""
tests/test_composite_alpha_scorer.py
======================================
Tests for the CompositeAlphaScorer (FS §4.3).

Validates:
  - Sub-component weights: env=20%, str=30%, div=50%
  - IVP > 80 hard floor → env immediately returns -100
  - IVP < 20 env boost (+20)
  - PCR > 1.3 structural penalty (-20)
  - PCR < 0.7 structural reward (+20)
  - Time-of-day multiplier (0.5x during 11:30-13:30 IST and after 15:00)
  - Total alpha clamped to [-100, +100]
  - Price↑ + PCR↓ divergence → div penalized (-50)
  - Price↓ + CVD↑ divergence → div rewarded (+50)
"""
import pytest
from unittest.mock import patch
from datetime import datetime


def make_scorer():
    from daemons.market_sensor import CompositeAlphaScorer
    return CompositeAlphaScorer()


# ══════════════════════════════════════════════════════════════════════════════
# A. Sub-component weights (FS §4.3)
# ══════════════════════════════════════════════════════════════════════════════

class TestScorerWeights:
    """Ensures the scorer is configured with the correct portfolio weights."""

    def test_env_weight(self):
        s = make_scorer()
        assert s.weights["env"] == pytest.approx(0.20), "Environmental weight must be 20%"

    def test_str_weight(self):
        s = make_scorer()
        assert s.weights["str"] == pytest.approx(0.30), "Structural weight must be 30%"

    def test_div_weight(self):
        s = make_scorer()
        assert s.weights["div"] == pytest.approx(0.50), "Divergence weight must be 50%"

    def test_weights_sum_to_one(self):
        s = make_scorer()
        total = sum(s.weights.values())
        assert total == pytest.approx(1.0), "All weights must sum to 1.0"


# ══════════════════════════════════════════════════════════════════════════════
# B. IVP hard floor (FS §4.3)
# ══════════════════════════════════════════════════════════════════════════════

class TestIVPFloor:
    """IVP > 80 collapses env sub-score to -100, which tanks total alpha."""

    def test_ivp_above_80_returns_negative_100_env(self):
        s = make_scorer()
        env_score = s._calc_env({"ivp": 85, "fii_bias": 50, "vix_slope": -1})
        assert env_score == pytest.approx(-100.0), "IVP > 80 must hard-floor env to -100"

    def test_ivp_below_80_allows_positive_env(self):
        s = make_scorer()
        env_score = s._calc_env({"ivp": 50, "fii_bias": 10, "vix_slope": 0})
        assert env_score > -100.0

    def test_ivp_below_20_adds_boost(self):
        s = make_scorer()
        env_no_boost = s._calc_env({"ivp": 50, "fii_bias": 0, "vix_slope": 0})
        env_with_boost = s._calc_env({"ivp": 15, "fii_bias": 0, "vix_slope": 0})
        assert env_with_boost == pytest.approx(env_no_boost + 20), "IVP < 20 must add +20 to env"


# ══════════════════════════════════════════════════════════════════════════════
# C. PCR scoring bands (FS §4.3)
# ══════════════════════════════════════════════════════════════════════════════

class TestPCRBands:
    """High PCR = bearish penalty; Low PCR = bullish reward."""

    def test_high_pcr_penalizes_str_score(self):
        s = make_scorer()
        normal = s._calc_str({"basis_slope": 0, "dist_max_pain": 0, "pcr": 1.0})
        penalized = s._calc_str({"basis_slope": 0, "dist_max_pain": 0, "pcr": 1.4})
        assert penalized == pytest.approx(normal - 20), "PCR > 1.3 must subtract 20 from str score"

    def test_low_pcr_rewards_str_score(self):
        s = make_scorer()
        normal = s._calc_str({"basis_slope": 0, "dist_max_pain": 0, "pcr": 1.0})
        rewarded = s._calc_str({"basis_slope": 0, "dist_max_pain": 0, "pcr": 0.6})
        assert rewarded == pytest.approx(normal + 20), "PCR < 0.7 must add 20 to str score"


# ══════════════════════════════════════════════════════════════════════════════
# D. Divergence sub-score (FS §4.3)
# ══════════════════════════════════════════════════════════════════════════════

class TestDivergenceScore:
    """Price/CVD/PCR slope divergence signals hidden distribution or accumulation."""

    def test_price_up_pcr_down_is_bearish_divergence(self):
        s = make_scorer()
        score = s._calc_div({"price_slope": 1, "pcr_slope": -1, "cvd_slope": 0})
        assert score == pytest.approx(-50.0), "Price ↑ + PCR ↓ should penalize -50"

    def test_price_down_cvd_up_is_bullish_divergence(self):
        s = make_scorer()
        score = s._calc_div({"price_slope": -1, "pcr_slope": 0, "cvd_slope": 1})
        assert score == pytest.approx(+50.0), "Price ↓ + CVD ↑ should reward +50"

    def test_no_divergence_returns_zero(self):
        s = make_scorer()
        score = s._calc_div({"price_slope": 0, "pcr_slope": 0, "cvd_slope": 0})
        assert score == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════════════
# E. Time-of-day multiplier (FS §4.3)
# ══════════════════════════════════════════════════════════════════════════════

class TestTimeMultiplier:
    """Alpha is halved during midday lull (11:30-13:30) and after 15:00."""

    def _score_at_hour(self, hour: int, minute: int = 0) -> float:
        """Compute a non-zero alpha at a given time."""
        s = make_scorer()
        env_data = {"fii_bias": 10, "vix_slope": 0, "ivp": 50}
        str_data = {"basis_slope": 0, "dist_max_pain": 0, "pcr": 1.0}
        div_data = {"price_slope": -1, "pcr_slope": 0, "cvd_slope": 1}
        fake_now = datetime(2024, 1, 15, hour, minute)
        with patch("daemons.market_sensor.datetime") as m:
            m.now.return_value = fake_now
            return s.get_total_score(env_data, str_data, div_data)

    def test_midday_lull_halves_alpha(self):
        score_normal = self._score_at_hour(10, 0)   # 10:00 — active session
        score_lunch = self._score_at_hour(12, 0)    # 12:00 — lunch lull
        assert abs(score_lunch) == pytest.approx(abs(score_normal) * 0.5, rel=0.01)

    def test_post_1500_halves_alpha(self):
        score_normal = self._score_at_hour(10, 0)
        score_eod = self._score_at_hour(15, 15)
        assert abs(score_eod) == pytest.approx(abs(score_normal) * 0.5, rel=0.01)

    def test_active_session_uses_full_alpha(self):
        score_9am = self._score_at_hour(9, 30)
        score_10am = self._score_at_hour(10, 0)
        assert abs(score_9am) == pytest.approx(abs(score_10am), rel=0.01)

    def test_alpha_clamped_to_100(self):
        s = make_scorer()
        env_data = {"fii_bias": 500, "vix_slope": 0, "ivp": 50}
        str_data = {"basis_slope": 100, "dist_max_pain": 0, "pcr": 0.5}
        div_data = {"price_slope": 0, "pcr_slope": 0, "cvd_slope": 0}
        with patch("daemons.market_sensor.datetime") as m:
            m.now.return_value = datetime(2024, 1, 15, 10, 0)
            score = s.get_total_score(env_data, str_data, div_data)
        assert score <= 100.0 and score >= -100.0, "Alpha must be clamped to [-100, 100]"
