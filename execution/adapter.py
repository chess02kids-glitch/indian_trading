"""Execution adapter protocol.

An adapter is the only component that "talks to a broker". The paper
adapter simulates fills deterministically; a future live adapter (a
separate, human-approved project) must implement this same protocol and
pass every order through :func:`execution.validation.validate_order_intent`
and the risk guard before submission.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from models.domain import OrderIntent, OrderResult, Position

__all__ = ["ExecutionAdapter"]


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Broker-side interface. No adapter may accept non-LIMIT orders."""

    def submit_order(self, intent: OrderIntent, reference_price: float) -> OrderResult:
        """Submit one validated order; returns the (possibly partial) fill."""
        ...

    def cancel_order(self, internal_order_id: str) -> OrderResult | None:
        """Cancel an open order; returns the terminal result or None."""
        ...

    def get_order_status(self, internal_order_id: str) -> OrderResult | None:
        """Return the latest result for an order, if known."""
        ...

    def get_positions(self) -> list[Position]:
        """Return current positions."""
        ...

    def get_open_orders(self) -> list[OrderResult]:
        """Return orders still awaiting a final state."""
        ...
