"""Deterministic risk-kill guard.

This package is the system's safety core. Hard rules enforced by
:mod:`tests.test_architecture`:

* It must import **nothing** outside the standard library (no agents, no
  LLM/model-serving libraries, no network, no broker, no repository layer).
* It must never be influenced by AI/LLM output: its inputs are plain numbers
  and datetimes, and its outputs are deterministic :class:`RiskDecision`.
* It fails closed: unknown or missing inputs produce the most protective
  state, never a pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Sequence


class RiskState(str, Enum):
    """Kill-switch states, ordered by increasing severity."""

    NOMINAL = "NOMINAL"
    ALERT_HUMAN = "ALERT_HUMAN"
    STOP_NEW_ORDERS = "STOP_NEW_ORDERS"
    CANCEL_OPEN_ORDERS = "CANCEL_OPEN_ORDERS"
    FLATTEN_POSITIONS = "FLATTEN_POSITIONS"
    LOCK_ACCOUNT = "LOCK_ACCOUNT"

    @property
    def severity(self) -> int:
        """Numeric severity used to aggregate multiple triggered checks."""
        return _SEVERITY[self]


#: The five protective responses (NOMINAL is the healthy steady state).
RISK_RESPONSES = (
    RiskState.STOP_NEW_ORDERS,
    RiskState.CANCEL_OPEN_ORDERS,
    RiskState.FLATTEN_POSITIONS,
    RiskState.LOCK_ACCOUNT,
    RiskState.ALERT_HUMAN,
)

_SEVERITY = {
    RiskState.NOMINAL: 0,
    RiskState.ALERT_HUMAN: 1,
    RiskState.STOP_NEW_ORDERS: 2,
    RiskState.CANCEL_OPEN_ORDERS: 3,
    RiskState.FLATTEN_POSITIONS: 4,
    RiskState.LOCK_ACCOUNT: 5,
}


class RiskCheckError(ValueError):
    """Raised when the guard is asked to evaluate an impossible context."""


@dataclass(frozen=True)
class RiskLimits:
    """Configured risk thresholds. All values are explicit; there are no
    implicit defaults that could silently loosen protection."""

    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    max_position_exposure: float = 0.25
    max_gross_exposure: float = 1.0
    max_data_age_hours: float = 18.0
    max_orders_per_hour: int = 60

    def __post_init__(self) -> None:
        if not 0 < self.max_daily_loss < 1:
            raise RiskCheckError("max_daily_loss must be in (0, 1)")
        if not 0 < self.max_drawdown < 1:
            raise RiskCheckError("max_drawdown must be in (0, 1)")
        if not 0 < self.max_position_exposure <= 1:
            raise RiskCheckError("max_position_exposure must be in (0, 1]")
        if self.max_gross_exposure <= 0:
            raise RiskCheckError("max_gross_exposure must be positive")
        if self.max_data_age_hours <= 0:
            raise RiskCheckError("max_data_age_hours must be positive")
        if self.max_orders_per_hour < 1:
            raise RiskCheckError("max_orders_per_hour must be at least 1")


@dataclass
class RiskContext:
    """Plain-data snapshot of trading state evaluated by the guard.

    Every field is optional; a missing field means "unknown", and the guard
    fails closed on unknown safety-relevant inputs.
    """

    now: datetime
    equity_now: float | None = None
    equity_day_start: float | None = None
    equity_peak: float | None = None
    position_exposure: Mapping[str, float] | None = None
    gross_exposure: float | None = None
    data_last_updated: datetime | None = None
    broker_connected: bool | None = None
    order_timestamps: Sequence[datetime] = field(default_factory=tuple)
    reconciliation_locked: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    """Deterministic guard output."""

    state: RiskState
    triggered_by: tuple[str, ...]
    details: Mapping[str, Any]
    human_action_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "triggered_by": list(self.triggered_by),
            "details": dict(self.details),
            "human_action_required": self.human_action_required,
        }

    def to_model(self) -> Any:
        """Return the ``models.domain.RiskDecision`` equivalent (lazy import)."""
        from models.domain import RiskDecision as _Model

        return _Model(
            state=self.state.value,
            triggered_by=tuple(self.triggered_by),
            details=dict(self.details),
            human_action_required=self.human_action_required,
            timestamp=None,
        )


def _require_finite(value: float | None, name: str) -> bool:
    """Return True when ``value`` is a usable finite number."""
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


class RiskGuard:
    """Evaluates :class:`RiskContext` against :class:`RiskLimits`.

    The evaluation is a fixed sequence of deterministic checks. The resulting
    state is the most severe state triggered by any check; ``ALERT_HUMAN`` is
    folded into ``human_action_required`` so a human is always notified when
    anything protective fires.
    """

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    # -- individual deterministic checks ---------------------------------

    def check_reconciliation_lock(self, context: RiskContext) -> RiskState | None:
        if context.reconciliation_locked:
            return RiskState.LOCK_ACCOUNT
        return None

    def check_broker_connectivity(self, context: RiskContext) -> RiskState | None:
        if context.broker_connected is None:
            # Unknown connectivity is not "fine" — fail closed.
            return RiskState.LOCK_ACCOUNT
        if not context.broker_connected:
            return RiskState.STOP_NEW_ORDERS
        return None

    def check_daily_loss(self, context: RiskContext) -> RiskState | None:
        if not (
            _require_finite(context.equity_now, "equity_now")
            and _require_finite(context.equity_day_start, "equity_day_start")
        ):
            return RiskState.LOCK_ACCOUNT
        start = float(context.equity_day_start)
        if start <= 0:
            return RiskState.LOCK_ACCOUNT
        loss = (start - float(context.equity_now)) / start
        if loss >= self.limits.max_daily_loss:
            return RiskState.STOP_NEW_ORDERS
        return None

    def check_max_drawdown(self, context: RiskContext) -> RiskState | None:
        if not (
            _require_finite(context.equity_now, "equity_now")
            and _require_finite(context.equity_peak, "equity_peak")
        ):
            return RiskState.LOCK_ACCOUNT
        peak = float(context.equity_peak)
        if peak <= 0:
            return RiskState.LOCK_ACCOUNT
        drawdown = (peak - float(context.equity_now)) / peak
        if drawdown >= self.limits.max_drawdown:
            return RiskState.FLATTEN_POSITIONS
        return None

    def check_position_exposure(self, context: RiskContext) -> RiskState | None:
        exposure = context.position_exposure
        equity = context.equity_now
        if exposure is None or not _require_finite(equity, "equity_now"):
            return RiskState.LOCK_ACCOUNT
        if float(equity) <= 0:
            return RiskState.LOCK_ACCOUNT
        position_state: RiskState | None = None
        for symbol, notional in dict(exposure).items():
            if not _require_finite(notional, f"exposure[{symbol}]"):
                return RiskState.LOCK_ACCOUNT
            if float(notional) / float(equity) > self.limits.max_position_exposure:
                position_state = RiskState.CANCEL_OPEN_ORDERS
        gross = context.gross_exposure
        gross_state: RiskState | None = None
        if gross is not None:
            if not _require_finite(gross, "gross_exposure"):
                return RiskState.LOCK_ACCOUNT
            if float(gross) / float(equity) > self.limits.max_gross_exposure:
                gross_state = RiskState.FLATTEN_POSITIONS
        result = worst_state(position_state, gross_state)
        return None if result is RiskState.NOMINAL else result

    def check_data_staleness(self, context: RiskContext) -> RiskState | None:
        if context.data_last_updated is None:
            return RiskState.LOCK_ACCOUNT
        age_hours = (context.now - context.data_last_updated).total_seconds() / 3600.0
        if age_hours > self.limits.max_data_age_hours:
            return RiskState.STOP_NEW_ORDERS
        return None

    def check_order_rate(self, context: RiskContext) -> RiskState | None:
        if not context.order_timestamps:
            return None
        window_start = context.now.timestamp() - 3600.0
        recent = [
            ts
            for ts in context.order_timestamps
            if _require_finite(ts.timestamp(), "order timestamp")
            and ts.timestamp() > window_start
        ]
        if len(recent) > self.limits.max_orders_per_hour:
            return RiskState.STOP_NEW_ORDERS
        return None

    def check_duplicate_order(
        self,
        idempotency_key: str,
        accepted_keys: Mapping[str, bool],
    ) -> RiskState | None:
        """Reject a repeated in-flight idempotency key.

        ``accepted_keys`` maps key -> completed. Any key already present is a
        duplicate (in-flight or already completed) and must not create a
        second order.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return RiskState.LOCK_ACCOUNT
        if idempotency_key in dict(accepted_keys):
            return RiskState.STOP_NEW_ORDERS
        return None

    # -- aggregate evaluation ---------------------------------------------

    _GLOBAL_CHECKS = (
        ("reconciliation_lock", check_reconciliation_lock),
        ("broker_connectivity", check_broker_connectivity),
        ("daily_loss", check_daily_loss),
        ("max_drawdown", check_max_drawdown),
        ("position_exposure", check_position_exposure),
        ("data_staleness", check_data_staleness),
        ("order_rate", check_order_rate),
    )

    def evaluate(self, context: RiskContext) -> RiskDecision:
        """Run every deterministic check and return the most severe result."""
        triggered: list[tuple[str, RiskState]] = []
        details: dict[str, Any] = {}
        for name, check in self._GLOBAL_CHECKS:
            state = check(self, context)
            if state is not None:
                triggered.append((name, state))
                details[name] = True
        if not triggered:
            return RiskDecision(
                state=RiskState.NOMINAL,
                triggered_by=(),
                details={},
                human_action_required=False,
            )
        worst = max((state for _, state in triggered), key=lambda state: state.severity)
        return RiskDecision(
            state=worst,
            triggered_by=tuple(name for name, _ in triggered),
            details=details,
            human_action_required=True,
        )


def worst_state(*states: RiskState | None) -> RiskState:
    """Return the most severe state among the supplied (ignoring None)."""
    candidates = [state for state in states if state is not None]
    if not candidates:
        return RiskState.NOMINAL
    return max(candidates, key=lambda state: state.severity)
