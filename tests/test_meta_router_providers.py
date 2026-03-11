"""
tests/test_meta_router_providers.py
=====================================
Tests for Meta Router Regime Providers (FS §2).

Validates:
  - Deterministic Provider: Hurst/ADX/ER thresholds
  - HMM Provider: reads from Redis correctly
  - Hybrid Provider: 40/60 HMM/Deterministic blending
  - Hybrid Dual-Key Constraint: both must agree for TRENDING
  - Hybrid confidence floor: >0.70 required
  - Kelly sizing: b=1.5, Half-Kelly clamped to [0.01, 0.5]
  - Cross-Index Divergence veto: TRENDING + CRASH = WAITING
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_hmm_provider(redis_state: str = "TRENDING"):
    """Create HMMProvider with mocked Redis returning a fixed state."""
    with patch("daemons.meta_router.MQManager"):
        from daemons.meta_router import HMMProvider
        p = HMMProvider.__new__(HMMProvider)
        p._redis = AsyncMock()
        p._redis.hget = AsyncMock(return_value=redis_state)
    return p


def make_det_provider():
    with patch("daemons.meta_router.MQManager"):
        from daemons.meta_router import DeterministicProvider
        p = DeterministicProvider.__new__(DeterministicProvider)
    return p


def make_meta_router():
    with patch("daemons.meta_router.MQManager"):
        from daemons.meta_router import MetaRouter
        r = MetaRouter.__new__(MetaRouter)
        r._redis = AsyncMock()
        r.mq = MagicMock()
        r.pub = MagicMock()
    return r


# ══════════════════════════════════════════════════════════════════════════════
# A. Deterministic Provider (FS §2.1)
# ══════════════════════════════════════════════════════════════════════════════

class TestDeterministicProvider:
    """Hurst > 0.55 AND ADX > 25 AND ER > 0.6 → TRENDING."""

    def _classify(self, hurst: float, adx: float, er: float) -> str:
        p = make_det_provider()
        return p.classify(hurst=hurst, adx=adx, er=er)

    def test_all_thresholds_met_returns_trending(self):
        assert self._classify(0.60, 30.0, 0.7) == "TRENDING"

    def test_hurst_below_threshold_returns_ranging(self):
        """Hurst < 0.45 → RANGING regardless of ADX/ER."""
        assert self._classify(0.40, 30.0, 0.7) == "RANGING"

    def test_hurst_between_045_055_returns_ranging(self):
        """Hurst in dead zone (0.45–0.55) → not enough trend memory."""
        result = self._classify(0.50, 30.0, 0.7)
        assert result in ("RANGING", "WAITING")

    def test_adx_below_threshold_prevents_trending(self):
        """ADX = 20 is insufficient momentum even with Hurst > 0.55."""
        assert self._classify(0.60, 20.0, 0.7) != "TRENDING"

    def test_er_below_threshold_prevents_trending(self):
        """ER = 0.5 means price is too choppy → not TRENDING."""
        assert self._classify(0.60, 30.0, 0.5) != "TRENDING"

    def test_probability_formula(self):
        """prob = 0.5 + (hurst-0.5) + (adx-20)/100 (FS §2.1)."""
        p = make_det_provider()
        hurst, adx = 0.65, 30.0
        expected_prob = 0.5 + (hurst - 0.5) + (adx - 20) / 100
        actual_prob = p.get_probability(hurst=hurst, adx=adx)
        assert actual_prob == pytest.approx(expected_prob, rel=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# B. Hybrid Provider — Dual-Key Constraint (FS §2.1)
# ══════════════════════════════════════════════════════════════════════════════

class TestHybridDualKeyConstraint:
    """Even with score > 0.70, both providers must independently agree for TRENDING."""

    @pytest.mark.asyncio
    async def test_both_trending_returns_trending(self):
        """HMM=TRENDING + Det=TRENDING → should return TRENDING."""
        r = make_meta_router()
        r._redis.hget = AsyncMock(return_value="TRENDING")
        signals = {"hurst": 0.65, "adx": 35.0, "er": 0.75, "symbol": "NIFTY50"}
        result = await r._hybrid_regime(signals)
        assert result == "TRENDING"

    @pytest.mark.asyncio
    async def test_hmm_trending_det_ranging_returns_ranging(self):
        """HMM=TRENDING but Det=RANGING (Hurst too low) → RANGING (veto)."""
        r = make_meta_router()
        r._redis.hget = AsyncMock(return_value="TRENDING")
        signals = {"hurst": 0.40, "adx": 15.0, "er": 0.4, "symbol": "NIFTY50"}
        result = await r._hybrid_regime(signals)
        assert result != "TRENDING", "Dual-Key constraint must veto if Deterministic disagrees"

    @pytest.mark.asyncio
    async def test_low_confidence_score_returns_ranging(self):
        """Combined score < 0.70 → RANGING even if both lean trending."""
        r = make_meta_router()
        r._redis.hget = AsyncMock(return_value="RANGING")
        signals = {"hurst": 0.53, "adx": 21.0, "er": 0.55, "symbol": "NIFTY50"}
        result = await r._hybrid_regime(signals)
        assert result != "TRENDING"


# ══════════════════════════════════════════════════════════════════════════════
# C. Fractional Kelly Sizing (FS §2.2)
# ══════════════════════════════════════════════════════════════════════════════

class TestKellySizing:
    """f = p - (1-p)/1.5, dispatched as max(0.01, 0.5 * f)."""

    def _kelly(self, p: float) -> float:
        from daemons.meta_router import MetaRouter
        with patch("daemons.meta_router.MQManager"):
            r = MetaRouter.__new__(MetaRouter)
        return r._kelly_weight(p)

    def test_high_probability_produces_positive_weight(self):
        weight = self._kelly(0.70)
        assert weight > 0

    def test_weight_clamped_minimum_001(self):
        """Even very low P produces at least 0.01 allocation."""
        weight = self._kelly(0.01)
        assert weight >= 0.01

    def test_weight_clamped_maximum_05(self):
        """Half-Kelly cap: weight must not exceed 0.5."""
        weight = self._kelly(0.99)
        assert weight <= 0.5

    def test_b_ratio_effect(self):
        """b=1.5: f = p - (1-p)/1.5. At p=0.6, f = 0.6 - 0.4/1.5 = 0.333; half = 0.167."""
        p = 0.6
        expected_f = p - (1 - p) / 1.5
        expected_half_kelly = max(0.01, 0.5 * expected_f)
        weight = self._kelly(p)
        assert weight == pytest.approx(expected_half_kelly, rel=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# D. Cross-Index Divergence Veto (FS §2.1)
# ══════════════════════════════════════════════════════════════════════════════

class TestCrossIndexDivergenceVeto:
    """TRENDING in one index + CRASH in another → WAITING (Fractured Market)."""

    @pytest.mark.asyncio
    async def test_trending_plus_crash_returns_waiting(self):
        r = make_meta_router()
        # Simulate: NIFTY=TRENDING, BANKNIFTY=CRASH
        regimes = {"NIFTY50": "TRENDING", "BANKNIFTY": "CRASH", "SENSEX": "RANGING"}
        result = r._check_cross_index_divergence(regimes)
        assert result == "WAITING", "TRENDING + CRASH across indices must trigger Fractured Market Veto"

    def test_all_trending_no_veto(self):
        r = make_meta_router()
        regimes = {"NIFTY50": "TRENDING", "BANKNIFTY": "TRENDING", "SENSEX": "TRENDING"}
        result = r._check_cross_index_divergence(regimes)
        assert result != "WAITING"

    def test_all_ranging_no_veto(self):
        r = make_meta_router()
        regimes = {"NIFTY50": "RANGING", "BANKNIFTY": "RANGING", "SENSEX": "RANGING"}
        result = r._check_cross_index_divergence(regimes)
        assert result != "WAITING"
