"""
Gamma Scalping Knight — Delta Hedging Specialist
Continuous delta-neutral rebalancing on option positions.
"""

import re
import logging
from datetime import datetime, timezone

from core.strategy.base_knight import BaseKnight, SignalSnapshot, OrderIntent
from core.greeks import BlackScholes

logger = logging.getLogger("GammaScalper")


class GammaScalperKnight(BaseKnight):
    """Delta-hedging specialist that maintains gamma-neutral positions."""

    def __init__(self, strategy_id: str, symbols: list[str], **config):
        super().__init__(strategy_id, symbols, **config)
        self.strike = float(config.get("strike", 22000))
        self.expiry_years = float(config.get("expiry_days", 30)) / 365.0
        self.iv = float(config.get("iv", 0.15))
        self.r = float(config.get("risk_free_rate", config.get("r", 0.065)))
        self.hedge_threshold = float(config.get("hedge_threshold", 0.10))
        self.last_calc_time = datetime.now(timezone.utc)
        self.bs = BlackScholes()

    @property
    def lifecycle_class(self) -> str:
        return "KINETIC"  # Gamma scalping uses kinetic lifecycle

    @property
    def target_regimes(self) -> list[int]:
        return [0, 1, 2, 3]  # Active in all regimes (hedging is always-on)

    def evaluate(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        price = signal.price
        now = datetime.now(timezone.utc)

        # Time decay
        dt = (now - self.last_calc_time).total_seconds() / (365.0 * 24 * 3600)
        self.expiry_years -= dt
        self.last_calc_time = now

        if self.expiry_years <= 0:
            self.expiry_years = 30.0 / 365.0
            return None

        # Dynamic strike from symbol
        dynamic_strike = self.strike
        if "CE" in symbol or "PE" in symbol:
            try:
                match = re.search(r'(\d+)(CE|PE)$', symbol)
                if match:
                    dynamic_strike = float(match.group(1))
            except Exception:
                pass

        otype = "call" if "CE" in symbol else "put"
        delta = self.bs.delta(price, dynamic_strike, self.expiry_years, self.r, self.iv, otype)

        # Target position: -delta * 100 lots
        target_pos = -delta * 100
        error = target_pos  # Simplified; registry tracks actual positions

        if abs(error) >= self.hedge_threshold:
            action = "BUY" if error > 0 else "SELL"
            return [OrderIntent(
                action=action,
                symbol=symbol,
                lifecycle_class="KINETIC",
                reason=f"GAMMA_HEDGE: delta={delta:.4f}, error={error:.2f}",
            )]
        return None
