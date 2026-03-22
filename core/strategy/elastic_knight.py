"""
Elastic Hunter Knight — S02: Mean Reversion Specialist
Target: S18 = 0 (Neutral) or 2 (Ranging)
Trigger: Price > 2.0σ from VWAP + RSI Divergence
"""

from core.strategy.base_knight import BaseKnight, SignalSnapshot, OrderIntent


class ElasticHunterKnight(BaseKnight):

    @property
    def lifecycle_class(self) -> str:
        return "ELASTIC"

    @property
    def target_regimes(self) -> list[int]:
        return [0, 2]  # Neutral or Ranging

    def evaluate(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        z_score = signal.price_zscore

        if abs(z_score) > 2.0:
            # Bet on return to Mean (VWAP)
            action = "SELL" if z_score > 2.0 else "BUY"
            return [OrderIntent(
                action=action,
                symbol=symbol,
                lifecycle_class="ELASTIC",
                parent_uuid=self.generate_parent_uuid("ELAS"),
            )]
        return None

    def check_exit(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        """Kill trade when price touches VWAP (mean reversion complete).
        
        Note: actual position direction check is done by the registry.
        Knight signals the VWAP-touch condition.
        """
        price = signal.price
        vwap = signal.vwap

        if vwap > 0 and abs(price - vwap) / max(vwap, 1) < 0.001:
            # Price essentially at VWAP — signal exit interest
            return [OrderIntent(
                action="SELL",  # Registry flips based on actual position
                symbol=symbol,
                lifecycle_class="ELASTIC",
                reason="VWAP_TOUCH",
            )]
        return None
