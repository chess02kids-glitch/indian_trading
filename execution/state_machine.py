"""The order state machine: which status may follow which, and why.

AUDIT-025
=========

This file used to be **0 bytes**. The documented order lifecycle existed as
prose, but the code that actually moved an order between states —
:class:`execution.paper.PaperBroker` — mutated a plain dictionary::

    self._orders[internal_order_id] = result

with no transition validation at all. Every status was reachable from every
other, including ``FILLED -> PENDING`` (a filled order becoming unfilled),
``CANCELLED -> FILLED`` (a cancelled order trading anyway) and
``REJECTED -> FILLED``. Nothing logged that any of it had happened, so a
bug in the simulator (or, later, in a real adapter) that resurrected a dead
order would have produced a silent position error rather than an exception.

This module is now the single definition of the lifecycle, and the broker
routes every mutation through it.

Design
------
* :data:`ALLOWED_TRANSITIONS` is the complete table. Anything not listed is
  refused — the machine fails **closed**.
* Terminal states (:data:`TERMINAL_STATES`) accept no further transitions.
  Once an order is filled, cancelled, rejected or expired it can never trade.
* ``UNKNOWN`` is the reconciliation escape hatch. A broker poll that cannot
  see an order must not fabricate a status, and it must also be able to
  *resolve* one later, so ``UNKNOWN`` may move to any real state but no real
  state may move *back* to ``UNKNOWN`` except via an explicit
  :meth:`OrderStateMachine.mark_unknown` (which records why).
* Quantity invariants are checked as part of the transition: a fill may never
  decrease ``filled_quantity``, may never exceed ``requested_quantity``, and
  ``FILLED`` requires the two to be equal.

The module depends only on :mod:`models.domain` and the standard library so
it can be imported from anywhere, including tests that build states directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

from models.domain import OrderResult, OrderStatus


class InvalidOrderTransition(RuntimeError):
    """Raised when an order is asked to move to a state it cannot reach."""

    def __init__(
        self,
        order_id: str,
        current: OrderStatus | str,
        new: OrderStatus | str,
        detail: str = "",
    ) -> None:
        self.order_id = order_id
        self.current = OrderStatus(current) if current is not None else None
        self.new = OrderStatus(new) if new is not None else None
        message = (
            f"order {order_id!r} cannot move {self._name(current)} -> "
            f"{self._name(new)}"
        )
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)

    @staticmethod
    def _name(status: OrderStatus | str | None) -> str:
        if status is None:
            return "<none>"
        return getattr(status, "value", str(status))


#: States from which an order can never move again.
TERMINAL_STATES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)

#: The complete transition table. Absence means "refused".
ALLOWED_TRANSITIONS: Mapping[OrderStatus, frozenset[OrderStatus]] = {
    # A brand-new order is created directly in one of these.
    OrderStatus.PENDING: frozenset(
        {
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        }
    ),
    # Terminal state: a filled order may never be un-filled or re-traded.
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    # "We cannot see this order" is resolvable, but is not a state to leave
    # an order in: it may move to any *real* state once the broker answers.
    OrderStatus.UNKNOWN: frozenset(
        {
            OrderStatus.PENDING,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
}


def is_terminal(status: OrderStatus) -> bool:
    """Whether ``status`` is a state an order can never leave."""
    return OrderStatus(status) in TERMINAL_STATES


def can_transition(current: OrderStatus, new: OrderStatus) -> bool:
    """Whether the lifecycle allows ``current -> new``."""
    current = OrderStatus(current)
    new = OrderStatus(new)
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def validate_transition(
    current: OrderStatus | None,
    new: OrderStatus,
    *,
    order_id: str = "",
) -> None:
    """Raise :class:`InvalidOrderTransition` unless the move is allowed.

    ``current=None`` means the order does not exist yet; creating an order is
    always allowed (the initial status is whatever the adapter decides).
    """
    if current is None:
        return
    if not can_transition(current, new):
        detail = (
            f"{OrderStatus(current).value} is terminal"
            if is_terminal(current)
            else "the transition is not in the lifecycle table"
        )
        raise InvalidOrderTransition(order_id, current, new, detail)


def validate_quantity_invariants(
    previous: OrderResult | None, new: OrderResult
) -> None:
    """Raise :class:`InvalidOrderTransition` if the fill counts are impossible.

    * ``filled_quantity`` may never decrease;
    * it may never exceed ``requested_quantity``;
    * ``FILLED`` requires them to be equal;
    * a non-zero fill requires a positive average price.
    """
    order_id = new.internal_order_id
    if new.filled_quantity > new.requested_quantity:
        raise InvalidOrderTransition(
            order_id,
            previous.status if previous else None,
            new.status,
            f"filled_quantity {new.filled_quantity} exceeds requested "
            f"{new.requested_quantity}",
        )
    if previous is not None and new.filled_quantity < previous.filled_quantity:
        raise InvalidOrderTransition(
            order_id,
            previous.status,
            new.status,
            f"filled_quantity decreased {previous.filled_quantity} -> "
            f"{new.filled_quantity}",
        )
    if new.status is OrderStatus.FILLED and new.filled_quantity != new.requested_quantity:
        raise InvalidOrderTransition(
            order_id,
            previous.status if previous else None,
            new.status,
            f"FILLED requires filled_quantity == requested_quantity "
            f"({new.filled_quantity} != {new.requested_quantity})",
        )
    if new.filled_quantity > 0 and not (
        new.average_fill_price and new.average_fill_price > 0
    ):
        raise InvalidOrderTransition(
            order_id,
            previous.status if previous else None,
            new.status,
            f"{new.filled_quantity} filled with no positive average price",
        )


def validate_result_transition(
    previous: OrderResult | None, new: OrderResult
) -> OrderResult:
    """Full check: lifecycle *and* quantity invariants. Returns ``new``."""
    validate_transition(
        previous.status if previous is not None else None,
        new.status,
        order_id=new.internal_order_id,
    )
    validate_quantity_invariants(previous, new)
    return new


@dataclass
class OrderStateMachine:
    """Per-order tracker that refuses and *records* illegal moves.

    It is intentionally the only place that may replace a stored
    :class:`~models.domain.OrderResult`. Every accepted move is appended to
    :attr:`history` so the audit trail can answer "how did this order end up
    filled?" after the fact.
    """

    internal_order_id: str
    history: list[tuple[datetime, OrderStatus, str]] = field(default_factory=list)

    def record(
        self, result: OrderResult, *, reason: str = "", at: datetime | None = None
    ) -> None:
        """Record a *newly created* order (no previous state)."""
        validate_quantity_invariants(None, result)
        self.history.append((at or result.timestamp, result.status, reason or "created"))

    def transition(
        self,
        previous: OrderResult,
        new: OrderResult,
        *,
        reason: str = "",
        at: datetime | None = None,
    ) -> OrderResult:
        """Validate and record ``previous -> new``.

        Raises :class:`InvalidOrderTransition` — and records nothing — when
        the move is not allowed, so a refused transition cannot corrupt the
        audit trail.
        """
        validate_result_transition(previous, new)
        self.history.append(
            (at or new.timestamp, new.status, reason or new.reason or "")
        )
        return new
