"""
Zero-DTE Knight — 0-DTE Theta Harvesting via Iron Butterfly
Target: Expiry day only, 09:30–10:30 IST window
Trigger: IV-RV Spread > 3%
"""

from datetime import datetime
from core.strategy.base_knight import BaseKnight, SignalSnapshot, OrderIntent
from core.options.chain_utils import get_index_meta, build_iron_butterfly


class ZeroDteKnight(BaseKnight):

    @property
    def lifecycle_class(self) -> str:
        return "ZERO_DTE"

    @property
    def target_regimes(self) -> list[int]:
        return [0, 1, 2, 3]  # Active in all regimes (time-based gating instead)

    def evaluate(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        now = datetime.now()
        meta = get_index_meta(symbol)

        # Only trade on the asset's expiry day
        current_day = now.strftime("%A")
        if current_day != meta["expiry_day"]:
            return None

        # 09:30–10:30 IST window
        current_time = now.hour * 60 + now.minute
        if current_time < 570 or current_time > 630:  # 09:30=570, 10:30=630
            return None

        # Tasty Edge Check: IV-RV Spread must be > 3%
        if signal.iv_rv_spread < 0.03:
            return None

        # Build Iron Butterfly via chain_utils
        strikes = build_iron_butterfly(signal.price, symbol, wing_offset_mult=4)
        parent_uuid = self.generate_parent_uuid("TT0")

        return [
            OrderIntent(action="SELL", otype="CE", strike=strikes["atm"],
                        parent_uuid=parent_uuid, lifecycle_class="ZERO_DTE", underlying=symbol),
            OrderIntent(action="BUY",  otype="CE", strike=strikes["call_wing"],
                        parent_uuid=parent_uuid, lifecycle_class="ZERO_DTE", underlying=symbol),
            OrderIntent(action="SELL", otype="PE", strike=strikes["atm"],
                        parent_uuid=parent_uuid, lifecycle_class="ZERO_DTE", underlying=symbol),
            OrderIntent(action="BUY",  otype="PE", strike=strikes["put_wing"],
                        parent_uuid=parent_uuid, lifecycle_class="ZERO_DTE", underlying=symbol),
        ]
