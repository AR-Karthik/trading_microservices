"""
Knight Registry — The "Thin Engine" orchestrator.
Dispatches SignalSnapshots to active Knights, applies vetoes, and routes OrderIntents.
"""

import json
import time
import uuid
import logging
import collections
from datetime import datetime, timezone

from core.strategy.base_knight import BaseKnight, SignalSnapshot, OrderIntent
from core.strategy.kinetic_hunter import KineticHunterKnight
from core.strategy.elastic_knight import ElasticHunterKnight
from core.strategy.positional_knight import PositionalKnight
from core.strategy.zero_dte import ZeroDteKnight
from core.strategy.gamma_scalper import GammaScalperKnight

logger = logging.getLogger("KnightRegistry")


# ── Knight Type Registry ──────────────────────────────────────────────────────

KNIGHT_CLASSES: dict[str, type[BaseKnight]] = {
    "KineticHunter": KineticHunterKnight,
    "ElasticHunter": ElasticHunterKnight,
    "PositionalHunter": PositionalKnight,         # sub_strategy=positional_hunter
    "DirectionalCredit": PositionalKnight,         # sub_strategy=credit_spread
    "CreditSpread": PositionalKnight,              # sub_strategy=credit_spread
    "IronCondor": PositionalKnight,                # sub_strategy=iron_condor
    "TastyTrade0DTE": ZeroDteKnight,
    "GammaScalping": GammaScalperKnight,
}

# Auto-set sub_strategy for Positional variants
_SUB_STRATEGY_MAP = {
    "DirectionalCredit": "credit_spread",
    "CreditSpread": "credit_spread",
    "IronCondor": "iron_condor",
    "PositionalHunter": "positional_hunter",
}


class KnightRegistry:
    """Orchestrates Knights: loads from config, dispatches signals, applies vetoes."""

    def __init__(self, redis_client, mq_manager=None, margin_manager=None):
        self._redis = redis_client
        self._mq = mq_manager
        self._margin = margin_manager
        self.active_knights: dict[str, BaseKnight] = {}
        self.strategy_states: dict[str, dict] = collections.defaultdict(
            lambda: {"lots": 1, "active": True}
        )
        # Position tracking (symbol → qty per strategy)
        self.positions: dict[str, dict[str, int]] = collections.defaultdict(
            lambda: collections.defaultdict(int)
        )
        self._lot_sizes_cache: dict[str, int] = {}
        self._lot_sizes_last_check = 0.0

    # ── Config Loading (from Redis) ───────────────────────────────────────

    async def sync_config(self):
        """Poll Redis for strategy configurations and sync active Knights."""
        try:
            configs = await self._redis.hgetall("active_strategies")
            current_ids = set()

            for strat_id, config_raw in configs.items():
                current_ids.add(strat_id)
                config = json.loads(config_raw)

                if strat_id not in self.active_knights:
                    if config.get("enabled", True):
                        strat_type = config.get("type", "")
                        cls = KNIGHT_CLASSES.get(strat_type)
                        if cls:
                            # Inject sub_strategy for Positional variants
                            if strat_type in _SUB_STRATEGY_MAP:
                                config["sub_strategy"] = _SUB_STRATEGY_MAP[strat_type]
                            symbols = config.get("symbols", [])
                            self.active_knights[strat_id] = cls(strat_id, symbols, **config)
                            logger.info(f"Loaded Knight: {strat_id} ({strat_type})")
                        else:
                            logger.warning(f"Unknown Knight type: {strat_type}")
                else:
                    if not config.get("enabled", True):
                        logger.info(f"Disabling Knight: {strat_id}")
                        del self.active_knights[strat_id]

            # Remove Knights not in config
            for stale in set(self.active_knights.keys()) - current_ids:
                logger.info(f"Unloading Knight: {stale}")
                del self.active_knights[stale]

        except Exception as e:
            logger.error(f"Config sync error: {e}")

    # ── Signal Dispatch ───────────────────────────────────────────────────

    async def dispatch(self, symbol: str, signal: SignalSnapshot) -> list[dict]:
        """Fan out a SignalSnapshot to all eligible Knights.
        
        Returns list of order dicts ready for MQ dispatch.
        """
        orders = []

        # Refresh lot sizes cache
        if time.time() - self._lot_sizes_last_check > 60:
            raw = await self._redis.hgetall("lot_sizes")
            if raw:
                self._lot_sizes_cache = {k: int(v) for k, v in raw.items()}
            self._lot_sizes_last_check = time.time()

        for s_id, knight in list(self.active_knights.items()):
            if symbol not in knight.symbols:
                continue

            if not knight.enabled or not knight.is_active_now():
                continue
            if not self.strategy_states[s_id]["active"]:
                continue

            # ── Global Vetoes ─────────────────────────────────────────
            if signal.is_toxic and signal.alpha_total < 0:
                continue
            if signal.halt_kinetic:
                continue
            if signal.macro_lockdown:
                continue

            # ── Regime Gate (automatic from Knight metadata) ──────────
            if not knight.regime_allows(signal):
                continue

            # ── Check Exit Before Entry ───────────────────────────────
            exit_intents = knight.check_exit(symbol, signal)
            if exit_intents:
                for intent in exit_intents:
                    order = self._intent_to_order(intent, knight, signal)
                    if order:
                        orders.append(order)
                continue  # Don't evaluate entry if exiting

            # ── Evaluate Entry ────────────────────────────────────────
            intents = knight.evaluate(symbol, signal)
            if not intents:
                continue

            for intent in intents:
                # ── Per-Intent Vetoes ─────────────────────────────────
                if not self._passes_vetoes(intent, knight, signal):
                    continue

                order = self._intent_to_order(intent, knight, signal)
                if order:
                    orders.append(order)

        return orders

    def _passes_vetoes(self, intent: OrderIntent, knight: BaseKnight,
                       signal: SignalSnapshot) -> bool:
        """Apply veto logic from the old run_strategies() monolith."""
        action = intent.action
        lifecycle = intent.lifecycle_class

        # Special actions bypass vetoes
        if action in ("HEDGE_REQUEST", "TRAP_ALERT"):
            return True

        # Alpha directional veto
        if action == "BUY" and signal.alpha_total < -20:
            logger.warning(f"⚠️ VETO BUY: {knight.strategy_id} rejected by negative Alpha ({signal.alpha_total:.1f})")
            return False
        if action == "SELL" and signal.alpha_total > 20:
            logger.warning(f"⚠️ VETO SELL: {knight.strategy_id} rejected by positive Alpha ({signal.alpha_total:.1f})")
            return False

        # Low Vol Trap for POSITIONAL
        if lifecycle == "POSITIONAL" and signal.iv_cache_pct < 12.0:
            logger.warning(f"⚠️ LOW VOL TRAP: {knight.strategy_id} blocked (IV {signal.iv_cache_pct:.1f}% < 12%)")
            return False

        # ASTO/Kinetic Filter
        if lifecycle == "KINETIC":
            if abs(signal.asto) <= 70 or signal.adx <= 25:
                return False

        # Quality Gate
        if signal.s27 < 30.0 and lifecycle != "POSITIONAL":
            logger.warning(f"⚠️ QUALITY VETO: {knight.strategy_id} rejected (S27 {signal.s27:.1f} < 30)")
            return False

        return True

    def _intent_to_order(self, intent: OrderIntent, knight: BaseKnight,
                         signal: SignalSnapshot) -> dict | None:
        """Convert OrderIntent to MQ order dict."""
        # S27 Quality-based sizing
        lots_from_router = self.strategy_states[knight.strategy_id].get("lots", 1)
        s27_mult = signal.s27 / 100.0
        qty = max(1, int(lots_from_router * s27_mult))

        order = {
            "order_id": str(uuid.uuid4()),
            "symbol": intent.symbol or signal.symbol,
            "action": intent.action,
            "quantity": intent.quantity or qty,
            "order_type": "MARKET",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "price": signal.price,
            "inception_spot": signal.inception_spot,
            "strategy_id": knight.strategy_id,
            "execution_type": knight.execution_type,
            "lifecycle_class": intent.lifecycle_class,
            "regime_snapshot": {
                "s18": signal.s18,
                "s27": signal.s27,
                "s26": signal.s26,
                "vpin": signal.vpin,
                "alpha": signal.alpha_total,
                "iv_rv": signal.iv_rv_spread,
                "asto": signal.asto,
                "z_score": signal.price_zscore,
                "whale_pivot": signal.whale_pivot,
            },
        }

        # Inject multi-leg fields
        if intent.otype:
            order["otype"] = intent.otype
        if intent.strike:
            order["strike"] = intent.strike
        if intent.parent_uuid:
            order["parent_uuid"] = intent.parent_uuid
        if intent.underlying:
            order["underlying"] = intent.underlying
        if intent.hedge_label:
            order["hedge_label"] = intent.hedge_label
        if intent.reason:
            order["reason"] = intent.reason
        if intent.side:
            order["side"] = intent.side

        return order

    # ── Position Updates ──────────────────────────────────────────────────

    def update_position(self, strat_id: str, symbol: str, qty_change: int):
        """Update position state after execution report."""
        if strat_id in self.active_knights:
            self.positions[strat_id][symbol] += qty_change
            logger.info(f"Position Updated: {strat_id} | {symbol} -> {self.positions[strat_id][symbol]}")

    def set_strategy_state(self, target: str, command: str, lots: int = 1):
        """Handle ACTIVATE/DEACTIVATE commands from Meta-Router."""
        if target == "ALL" or target is None:
            for s_id in self.active_knights:
                self.strategy_states[s_id]["active"] = command == "ACTIVATE"
                self.strategy_states[s_id]["lots"] = lots
        else:
            self.strategy_states[target]["active"] = command == "ACTIVATE"
            self.strategy_states[target]["lots"] = lots
