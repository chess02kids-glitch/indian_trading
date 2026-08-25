"""Core execution-domain models shared by execution, risk, and reconciliation.

These models are the safety boundary of the system. Every object that can lead
to an order (``OrderIntent``) is validated as ``LIMIT``-only, idempotent, and
paper/sandbox-mode before it may reach an execution adapter. Research-only
models (signals, factors, market panels) live in :mod:`research.contracts`.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Re-export the research signal so a single Signal definition is used system-wide.
from research.contracts import Signal  # noqa: F401  (re-exported by design)

__all__ = [
    "ExecutionMode",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "MarketBar",
    "OrderIntent",
    "OrderResult",
    "Position",
    "PortfolioTarget",
    "RiskDecision",
    "ReconciliationMismatch",
    "ReconciliationResult",
    "ResearchResult",
    "Signal",
    "UniverseMembership",
    "VALID_EXCHANGES",
]

#: Indian cash-market exchanges the system is permitted to trade.
VALID_EXCHANGES = frozenset({"NSE", "BSE"})

_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9&.\-+]{0,19}$")


class ExecutionMode(str, Enum):
    """Permitted operating modes.

    There is deliberately no ``LIVE`` member: production-capital execution is
    outside the system boundary. Adding one is an architecture change that
    requires a new ADR and human sign-off.
    """

    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SANDBOX = "SANDBOX"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """The only order type the system accepts.

    ``LIMIT`` is the sole member. Any other value (``"MARKET"``, ``"IOC"``,
    ``"LIMIT_WITH_MARKET"``, ...) is not a member of this enum and is rejected
    by :func:`execution.validation.validate_order_intent` before it can reach
    an adapter.
    """

    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class _FrozenModel(BaseModel):
    """Base for immutable, strict domain objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class MarketBar(_FrozenModel):
    """One validated OHLCV observation with provenance."""

    source: str
    symbol: str
    exchange: str
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    adj_close: float | None = None
    is_adjusted: bool = False
    corp_action_applied: bool = False
    ingested_at: datetime | None = None
    source_ts: datetime | None = None

    @field_validator("source", "symbol", "exchange")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @field_validator("exchange")
    @classmethod
    def _valid_exchange(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_EXCHANGES:
            raise ValueError(f"exchange must be one of {sorted(VALID_EXCHANGES)}")
        return normalized

    @field_validator("open", "high", "low", "close")
    @classmethod
    def _positive_price(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("prices must be finite and strictly positive")
        return value

    @field_validator("volume")
    @classmethod
    def _non_negative_volume(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("volume must be finite and non-negative")
        return value

    @field_validator("adj_close")
    @classmethod
    def _optional_adj_close(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("adj_close must be finite and strictly positive")
        return value

    def model_post_init(self, __context: Any) -> None:
        if self.high < max(self.open, self.close):
            raise ValueError("high must be >= max(open, close)")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= min(open, close)")
        if self.high < self.low:
            raise ValueError("high must be >= low")
        if self.is_adjusted and self.adj_close is None:
            raise ValueError("is_adjusted requires adj_close")
        if self.adj_close is not None:
            object.__setattr__(self, "is_adjusted", True)


class OrderIntent(_FrozenModel):
    """A validated, idempotent, LIMIT-only request to adjust a position.

    This is the only object an execution adapter may act on. Strategies and
    AI code never produce broker calls; they produce ``PortfolioTarget``
    objects that a deterministic layer converts into ``OrderIntent``.
    """

    internal_order_id: str
    idempotency_key: str
    strategy_id: str
    hypothesis_id: str
    symbol: str
    exchange: str = "NSE"
    side: OrderSide
    quantity: int
    limit_price: float
    order_type: OrderType = OrderType.LIMIT
    timestamp: datetime
    target_position: int | None = None

    @field_validator(
        "internal_order_id", "idempotency_key", "strategy_id", "hypothesis_id"
    )
    @classmethod
    def _identifier_non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("identifier must be a non-empty string")
        return value

    @field_validator("symbol")
    @classmethod
    def _valid_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _SYMBOL_RE.match(normalized):
            raise ValueError(f"invalid symbol: {value!r}")
        return normalized

    @field_validator("exchange")
    @classmethod
    def _valid_exchange(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_EXCHANGES:
            raise ValueError(f"exchange must be one of {sorted(VALID_EXCHANGES)}")
        return normalized

    @field_validator("quantity", "target_position")
    @classmethod
    def _positive_int(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("quantity must be a positive whole number of shares")
        return value

    @field_validator("limit_price")
    @classmethod
    def _positive_price(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("limit_price must be finite and strictly positive")
        return value

    @field_validator("order_type")
    @classmethod
    def _limit_only(cls, value: OrderType) -> OrderType:
        # OrderType has exactly one member, so any MARKET/IOC/other input is
        # rejected at construction time as well as at validation time.
        if value is not OrderType.LIMIT:
            raise ValueError("only LIMIT orders are permitted")
        return value


class OrderResult(_FrozenModel):
    """Outcome of submitting an ``OrderIntent`` to an execution adapter."""

    internal_order_id: str
    idempotency_key: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide | None = None
    status: OrderStatus
    requested_quantity: int
    filled_quantity: int = 0
    average_fill_price: float | None = None
    timestamp: datetime
    reason: str | None = None

    @field_validator("requested_quantity", "filled_quantity")
    @classmethod
    def _quantity_non_negative(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("quantities must be non-negative whole numbers")
        return value

    @field_validator("average_fill_price")
    @classmethod
    def _optional_price(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("average_fill_price must be finite and positive")
        return value


class Position(_FrozenModel):
    """Net position for one symbol on one exchange."""

    symbol: str
    exchange: str = "NSE"
    quantity: int = 0
    average_price: float | None = None
    target_quantity: int | None = None
    updated_at: datetime | None = None

    @field_validator("quantity", "target_quantity")
    @classmethod
    def _int_quantity(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("position quantity must be a non-negative whole number")
        return value

    @field_validator("average_price")
    @classmethod
    def _optional_price(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("average_price must be finite and positive")
        return value


class PortfolioTarget(_FrozenModel):
    """Deterministic desired end-state for one strategy at one date.

    Execution converts targets into ``OrderIntent`` objects (LIMIT orders at
    the supplied limit prices); the strategy itself never talks to a broker.
    """

    strategy_id: str
    hypothesis_id: str
    as_of: date
    limits: Mapping[str, float]
    target_quantities: Mapping[str, int] | None = None

    @field_validator("limits")
    @classmethod
    def _valid_limits(cls, value: Mapping[str, float]) -> dict[str, float]:
        limits = {
            str(symbol).strip().upper(): float(price)
            for symbol, price in dict(value).items()
        }
        for symbol, price in limits.items():
            if not _SYMBOL_RE.match(symbol):
                raise ValueError(f"invalid symbol in limits: {symbol!r}")
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"limit price for {symbol} must be positive")
        return limits

    @field_validator("target_quantities")
    @classmethod
    def _valid_targets(cls, value: Mapping[str, int] | None) -> dict[str, int] | None:
        if value is None:
            return None
        targets = {
            str(symbol).strip().upper(): int(quantity)
            for symbol, quantity in dict(value).items()
        }
        for symbol, quantity in targets.items():
            if not _SYMBOL_RE.match(symbol):
                raise ValueError(f"invalid symbol in targets: {symbol!r}")
            if quantity < 0:
                raise ValueError(f"target quantity for {symbol} must be >= 0")
        return targets


class RiskDecision(_FrozenModel):
    """Deterministic output of the risk-kill guard.

    ``state`` uses the ``risk_kill.RiskState`` vocabulary (serialized as a
    string so this model does not create an import dependency on risk_kill).
    """

    state: str
    triggered_by: tuple[str, ...] = ()
    details: Mapping[str, Any] = Field(default_factory=dict)
    human_action_required: bool = False
    timestamp: datetime | None = None

    @field_validator("state")
    @classmethod
    def _known_state(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("state must be a non-empty string")
        return value

    @field_validator("details")
    @classmethod
    def _plain_details(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class ReconciliationMismatch(_FrozenModel):
    """One detected difference between expected and broker/paper state."""

    kind: str
    symbol: str | None = None
    expected: Any = None
    actual: Any = None
    detail: str = ""


class ReconciliationResult(_FrozenModel):
    """Outcome of comparing expected state against execution state."""

    run_id: str
    as_of: datetime
    matched: bool
    mismatches: tuple[ReconciliationMismatch, ...] = ()
    locked: bool = False
    lock_reason: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None

    @field_validator("mismatches")
    @classmethod
    def _mismatch_tuple(
        cls, value: tuple[ReconciliationMismatch, ...]
    ) -> tuple[ReconciliationMismatch, ...]:
        return tuple(value)


class ResearchResult(_FrozenModel):
    """Persistable summary of one research experiment run."""

    hypothesis_id: str
    strategy_id: str
    status: str
    metrics: Mapping[str, float] = Field(default_factory=dict)
    dataset_version: str | None = None
    backtest_period: str | None = None
    oos_period: str | None = None
    cost_model: str | None = None
    run_id: str | None = None
    created_at: datetime | None = None
    dataset_fingerprint: str | None = None
    config_fingerprint: str | None = None
    code_fingerprint: str | None = None

    @field_validator("metrics")
    @classmethod
    def _numeric_metrics(cls, value: Mapping[str, float]) -> dict[str, float]:
        metrics = {}
        for key, item in dict(value).items():
            number = float(item)
            if not math.isfinite(number):
                raise ValueError(f"metric {key} must be finite")
            metrics[str(key)] = number
        return metrics

class UniverseMembership(_FrozenModel):
    """Historical universe membership to prevent survivorship bias."""

    symbol: str
    index_name: str
    valid_from: date
    valid_to: date | None = None

    @field_validator("symbol", "index_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    def is_member(self, target_date: date) -> bool:
        if target_date < self.valid_from:
            return False
        if self.valid_to is not None and target_date > self.valid_to:
            return False
        return True
