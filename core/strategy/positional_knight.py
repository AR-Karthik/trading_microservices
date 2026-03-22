"""
Positional Knight — Theta/Edge Harvesting
Merges: DirectionalCreditSpreadStrategy, CreditSpreadStrategy,
        IronCondorStrategy, PositionalHunterStrategy.
Target: S18 = 2 (Ranging) | S18 = 1 (Trending, for directional)
Trigger: IV-RV > 3% + Persistence > 60%
"""

import time
import logging
import re
from core.strategy.base_knight import BaseKnight, SignalSnapshot, OrderIntent
from core.options.chain_utils import (
    get_increment, snap_strike, build_credit_spread,
    build_iron_condor, find_strike_for_delta,
)
from core.greeks import BlackScholes

logger = logging.getLogger("PositionalKnight")


class PositionalKnight(BaseKnight):
    """Unified Positional lifecycle Knight.
    
    Sub-strategy selected by config['sub_strategy']:
        - 'iron_condor':       Neutral IC (4-leg)
        - 'credit_spread':     Directional credit spread (2-leg)
        - 'positional_hunter': Auto-directional from z-score (2-leg)
    """

    def __init__(self, strategy_id: str, symbols: list[str], **config):
        super().__init__(strategy_id, symbols, **config)
        self.sub_strategy = config.get("sub_strategy", "positional_hunter")
        self.spread_width = float(config.get("spread_width", 100))
        self.target_delta = float(config.get("target_delta", 0.25))
        self.wing_delta = float(config.get("wing_delta", 0.10))
        self.spread_delta = float(config.get("spread_delta", 0.15))
        self.expiry_years = float(config.get("expiry_days", 7)) / 365.0
        self.iv = float(config.get("iv", 0.15))
        self.side = config.get("side", "auto")  # "bull", "bear", or "auto"
        self.bs = BlackScholes()
        self._last_parent_uuid = "POS_INITIAL"

    @property
    def lifecycle_class(self) -> str:
        return "POSITIONAL"

    @property
    def target_regimes(self) -> list[int]:
        if self.sub_strategy == "credit_spread" and self.side != "auto":
            return [1, 2]  # Directional credit works in trending too
        return [2]  # Ranging

    def evaluate(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        if self.sub_strategy == "iron_condor":
            return self._eval_iron_condor(symbol, signal)
        elif self.sub_strategy == "credit_spread":
            return self._eval_credit_spread(symbol, signal)
        else:
            return self._eval_positional_hunter(symbol, signal)

    # ── Iron Condor (4-leg, delta-selected) ───────────────────────────────

    def _eval_iron_condor(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        price = signal.price
        asto = signal.asto
        r = 0.065

        # Extreme Trend Management (Hedge Hybrid)
        if abs(asto) >= 90:
            return self._handle_extreme_trend(symbol, signal, price, r)

        # Standard Iron Condor entry
        strikes = build_iron_condor(
            price, symbol, self.expiry_years, r, self.iv,
            self.wing_delta, self.spread_delta, self.bs,
        )
        parent_uuid = self.generate_parent_uuid("IC")
        self._last_parent_uuid = parent_uuid

        return [
            OrderIntent(action="BUY",  otype="PE", strike=strikes["put_wing"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL"),
            OrderIntent(action="SELL", otype="PE", strike=strikes["put_main"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL"),
            OrderIntent(action="SELL", otype="CE", strike=strikes["call_main"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL"),
            OrderIntent(action="BUY",  otype="CE", strike=strikes["call_wing"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL"),
        ]

    def _handle_extreme_trend(self, symbol: str, signal: SignalSnapshot,
                               price: float, r: float) -> list[OrderIntent]:
        """Hedge Hybrid: ASTO ±90 → hedge or trap alert."""
        asto = signal.asto
        whale = signal.whale_pivot
        is_aligned = (asto > 0 and whale > 0) or (asto < 0 and whale < 0)

        if is_aligned:
            # Net delta neutralization via micro-futures
            # Simplified: compute rough net delta (registry can refine)
            return [OrderIntent(
                action="HEDGE_REQUEST",
                symbol=f"{symbol}-FUT",
                quantity=1,
                parent_uuid=self._last_parent_uuid,
                hedge_label=f"HYBRID_{symbol}_{int(time.time())}",
                reason=f"WHALE_ALIGNED_HEDGE: ASTO={asto:.1f}, S22={whale:.1f}",
            )]
        else:
            return [OrderIntent(
                action="TRAP_ALERT",
                symbol=symbol,
                side="CALL" if asto > 0 else "PUT",
                reason=f"WHALE_MISALIGNED_TRAP: ASTO={asto:.1f}, S22={whale:.1f}",
            )]

    # ── Credit Spread (2-leg, directional) ────────────────────────────────

    def _eval_credit_spread(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        price = signal.price
        side = self.side

        # Auto-direction from alpha
        if side == "auto":
            alpha = signal.alpha_total
            if alpha > 25:
                side = "bull"
            elif alpha < -25:
                side = "bear"
            else:
                return None

        spread = build_credit_spread(price, side, symbol, offset=100.0, width=self.spread_width)
        parent_uuid = self.generate_parent_uuid("DCS")

        return [
            OrderIntent(action="SELL", otype=spread["otype"], strike=spread["short_strike"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL", underlying=symbol),
            OrderIntent(action="BUY",  otype=spread["otype"], strike=spread["long_strike"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL", underlying=symbol),
        ]

    # ── Positional Hunter (2-leg, auto-directional from z-score) ──────────

    def _eval_positional_hunter(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        # Double-gate: IV-RV > 3% + Persistence > 60%
        if signal.iv_rv_spread <= 0.03 or signal.s26 <= 60:
            return None

        z_score = signal.price_zscore
        side = "bull" if z_score < 0 else "bear"
        price = signal.price

        spread = build_credit_spread(price, side, symbol, offset=100.0, width=100.0)
        parent_uuid = self.generate_parent_uuid("POSI")

        return [
            OrderIntent(action="SELL", otype=spread["otype"], strike=spread["short_strike"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL", underlying=symbol),
            OrderIntent(action="BUY",  otype=spread["otype"], strike=spread["long_strike"],
                        parent_uuid=parent_uuid, lifecycle_class="POSITIONAL", underlying=symbol),
        ]
