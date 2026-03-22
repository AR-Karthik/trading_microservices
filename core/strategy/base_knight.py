"""
BaseKnight Interface + Data Types — Module 4 Decomposition
The Strategy Pattern interface for all trade entry/exit strategies.
"""

import uuid
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("Knights")


# ── Data Types ────────────────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class SignalSnapshot:
    """Immutable market signal state broadcast to all Knights.
    
    Created once per tick by SignalDispatcher. Frozen to prevent
    Knight-to-Knight signal contamination.
    """
    symbol: str = ""
    price: float = 0.0
    timestamp: str = ""

    # Regime (from RegimeShm)
    s18: int = 0             # 0:Neutral, 1:Trending, 2:Ranging, 3:Volatile
    s27: float = 100.0       # Quality (0-100)
    s26: float = 0.0         # Persistence (0-100)

    # Alpha (from SHM SignalVector)
    asto: float = 0.0
    alpha_total: float = 0.0  # S_Total
    smart_flow: float = 0.0
    whale_pivot: float = 0.0
    vpin: float = 0.0
    adx: float = 0.0
    atr: float = 20.0
    iv_rv_spread: float = 0.0
    iv_atm: float = 0.15

    # Derived
    price_zscore: float = 0.0
    vwap: float = 0.0
    hw_alphas: tuple = ()     # Power Five heavyweight alphas

    # Health
    latency_ms: float = 0.0
    is_toxic: bool = False    # Veto flag from SHM
    sequence_id: int = 0

    # Cached from Redis (refreshed every 5s by dispatcher)
    halt_kinetic: bool = False
    macro_lockdown: bool = False
    iv_cache_pct: float = 15.0   # IV as percentage (e.g., 15.0 = 15%)
    inception_spot: float = 0.0  # Current underlying spot


@dataclass(slots=True)
class OrderIntent:
    """Structured output from a Knight's evaluation.
    
    Replaces the raw dicts/strings that strategies returned before.
    The registry converts these into ORDER_REQUEST MQ messages.
    """
    action: str = "BUY"       # BUY | SELL | HEDGE_REQUEST | TRAP_ALERT
    symbol: str = ""
    otype: str = ""           # CE | PE | "" (for futures/equity)
    strike: float = 0.0
    quantity: int = 1
    lifecycle_class: str = "KINETIC"
    parent_uuid: str = ""
    underlying: str = ""

    # Optional metadata
    reason: str = ""
    hedge_label: str = ""
    side: str = ""            # For TRAP_ALERT: CALL | PUT


# ── BaseKnight ────────────────────────────────────────────────────────────────

class BaseKnight(ABC):
    """Abstract base for all strategy Knights.
    
    Knights are stateless evaluators — they receive a frozen SignalSnapshot
    and return OrderIntents. No I/O, no Redis, no SHM inside evaluate().
    """

    def __init__(self, strategy_id: str, symbols: list[str], **config):
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.config = config
        self.execution_type = config.get("execution_type", "Paper")
        self.schedule: dict = config.get("schedule", {})
        self._enabled = True

    @property
    @abstractmethod
    def lifecycle_class(self) -> str:
        """KINETIC | POSITIONAL | ZERO_DTE | ELASTIC"""
        ...

    @property
    @abstractmethod
    def target_regimes(self) -> list[int]:
        """S18 values where this Knight may fire. e.g. [1] for Trending."""
        ...

    @abstractmethod
    def evaluate(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        """Evaluate entry conditions.
        
        Returns list of OrderIntents for multi-leg entries, or None for no signal.
        """
        ...

    def check_exit(self, symbol: str, signal: SignalSnapshot) -> list[OrderIntent] | None:
        """Evaluate exit conditions (optional override). Default: no exit logic."""
        return None

    def regime_allows(self, signal: SignalSnapshot) -> bool:
        """Check if current regime permits this Knight to fire."""
        return signal.s18 in self.target_regimes

    def is_active_now(self) -> bool:
        """Check if Knight is within its scheduled operating window."""
        if not self.schedule:
            return True

        now = datetime.now()
        current_day = now.strftime("%A")
        current_time = now.strftime("%H:%M")

        days = self.schedule.get("days", [])
        if days and current_day not in days:
            return False

        slots = self.schedule.get("slots", [])
        if not slots and "start" in self.schedule and "end" in self.schedule:
            slots = [{"start": self.schedule["start"], "end": self.schedule["end"]}]

        if not slots:
            return True

        for slot in slots:
            if slot.get("start", "00:00") <= current_time <= slot.get("end", "23:59"):
                return True
        return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, val: bool):
        self._enabled = val

    def generate_parent_uuid(self, prefix: str = "K") -> str:
        """Utility: generate a prefixed UUID for multi-leg grouping."""
        return f"{prefix}_{uuid.uuid4().hex[:8]}"
