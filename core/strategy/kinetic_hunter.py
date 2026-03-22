"""
Kinetic Hunter Knight — S01: Trend Movement Specialist
Target: S18 = 1 (Trending)
Trigger: |ASTO| > 70 + Smart Flow > 20 + Whale Alignment
"""

from core.strategy.base_knight import BaseKnight, SignalSnapshot, OrderIntent


class KineticHunterKnight(BaseKnight):

    @property
    def lifecycle_class(self) -> str:
        return "KINETIC"

    @property
    def target_regimes(self) -> list[int]:
        return [1]  # Trending only

    def evaluate(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        asto = signal.asto
        smart_flow = signal.smart_flow
        whale_p = signal.whale_pivot

        # Kinetic triple-lock: |ASTO| > 70 + |SmartFlow| > 20 + Whale aligned
        if abs(asto) <= 70 or abs(smart_flow) <= 20:
            return None

        # Power Five Pulse Gate (3/5 heavyweights must confirm)
        hw = signal.hw_alphas
        if len(hw) >= 5:
            power_five = hw[:5]
            bullish_count = sum(1 for a in power_five if a > 10)
            bearish_count = sum(1 for a in power_five if a < -10)

            if asto > 70 and bullish_count < 3:
                return None
            if asto < -70 and bearish_count < 3:
                return None

        is_bullish = asto > 70 and smart_flow > 20 and whale_p > 0
        is_bearish = asto < -70 and smart_flow < -20 and whale_p < 0

        if is_bullish or is_bearish:
            action = "BUY" if is_bullish else "SELL"
            return [OrderIntent(
                action=action,
                symbol=symbol,
                lifecycle_class="KINETIC",
                parent_uuid=self.generate_parent_uuid("KINE"),
            )]
        return None

    def check_exit(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        """Whale Pivot (S22) flip = Momentum dead → exit."""
        whale_p = signal.whale_pivot
        # Delegate to registry to check position direction. Here we signal
        # a generic "alpha death" exit when whale flips.
        if whale_p == 0:
            return None
        # The registry checks actual positions and determines if this is a flip.
        # Knight only signals intent.
        return None
