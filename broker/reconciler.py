"""Sandbox reconciliation.

After every sandbox order and again at end-of-day, the broker's view is
diffed against local state through the existing
:class:`reconciliation.engine.ReconciliationEngine` — sandbox reconciliation
is not a new mechanism, it is the *same* kill-switch reconciliation applied
to a sandbox adapter. Any mismatch locks the account until a human resolves
it (ADR-008).

Flows:

* :meth:`SandboxReconciler.poll_order` — bounded, deterministic status
  polling of one order until it reaches a terminal state.
* :meth:`SandboxReconciler.sync_order` — poll + persist the latest result
  into the local order repository (update local state).
* :meth:`SandboxReconciler.reconcile_now` — detect mismatches between the
  expected state (local repositories) and the broker's actual state.
* :meth:`SandboxReconciler.end_of_day` — the mandatory end-of-day pass:
  reconciles and persists the outcome, deriving the risk decision that
  locks the account on mismatch.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Callable, Mapping

from broker.errors import BrokerTransportError
from broker.interface import BrokerAdapter
from broker.models import BrokerOrderRecord
from models.domain import (
    OrderResult,
    OrderStatus,
    ReconciliationResult,
)
from reconciliation.engine import ReconciliationEngine, ReconciliationInput
from risk_kill import RiskDecision, RiskGuard

__all__ = [
    "SandboxReconciler",
    "TERMINAL_STATUSES",
    "DEFAULT_MAX_POLLS",
    "record_to_result",
]

TERMINAL_STATUSES = (
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELLED,
    OrderStatus.EXPIRED,
)

_OPEN_STATUSES = (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)

DEFAULT_MAX_POLLS = 5
DEFAULT_POLL_INTERVAL = 0.2


def record_to_result(record: BrokerOrderRecord) -> OrderResult:
    """Map a broker order record to a domain result.

    The broker-side tag is the caller's internal order id; the idempotency
    key rides along when the payload carried one.
    """
    return OrderResult.model_validate(
        {
            "internal_order_id": record.tag or record.order_id,
            "idempotency_key": record.idempotency_key or record.tag or record.order_id,
            "broker_order_id": record.order_id,
            "symbol": record.symbol,
            "side": record.side,
            "status": record.status,
            "requested_quantity": record.quantity,
            "filled_quantity": record.filled_quantity,
            "average_fill_price": record.average_price,
            "timestamp": record.updated_at or record.placed_at or datetime.now(UTC),
            "reason": record.message,
        }
    )


class SandboxReconciler:
    """Polls sandbox orders and reconciles broker state against local state."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        order_repository: Any = None,
        position_repository: Any = None,
        reconciliation_repository: Any = None,
        engine: ReconciliationEngine | None = None,
        risk_guard: RiskGuard | None = None,
        max_polls: int = DEFAULT_MAX_POLLS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_polls < 1:
            raise ValueError("max_polls must be at least 1")
        self._adapter = adapter
        self._orders = order_repository
        self._positions = position_repository
        self._recon_repo = reconciliation_repository
        self._engine = engine or ReconciliationEngine(
            risk_guard=risk_guard or RiskGuard()
        )
        self._max_polls = int(max_polls)
        self._poll_interval = float(poll_interval_seconds)
        self._sleep = sleep or time.sleep

    # -- per-order polling ---------------------------------------------------

    def poll_order(self, order_ref: str) -> BrokerOrderRecord | None:
        """Poll one order until terminal or the poll budget is exhausted.

        Deterministic: at most ``max_polls`` broker calls with a fixed
        interval between them (the sleeper is injectable for tests).
        Transient transport errors consume one attempt and polling resumes;
        authentication errors propagate (the operator must re-auth).
        """
        latest: BrokerOrderRecord | None = None
        for attempt in range(self._max_polls):
            try:
                latest = self._adapter.get_order_status(order_ref)
            except BrokerTransportError:
                latest = None
            else:
                if latest is None:
                    return None
                if latest.status in TERMINAL_STATUSES:
                    return latest
            if attempt < self._max_polls - 1:
                self._sleep(self._poll_interval)
        return latest

    def sync_order(self, order_ref: str) -> OrderResult | None:
        """Poll an order and persist its latest result into local state.

        Also syncs the affected position once the order has fills, so the
        local position repository reflects the broker's truth.
        """
        record = self.poll_order(order_ref)
        if record is None:
            return None
        result = record_to_result(record)
        if self._orders is not None:
            self._orders.save_result(result)
        if self._positions is not None and record.filled_quantity > 0:
            for position in self._adapter.get_positions():
                self._positions.upsert_position(position)
        return result

    # -- reconciliation ---------------------------------------------------------

    def expected_from_local(self) -> Mapping[str, Any]:
        """Build the expected-state view from the local repositories."""
        positions: dict[str, int] = {}
        open_orders: dict[str, str] = {}
        filled: dict[str, int] = {}
        if self._positions is not None:
            for position in self._positions.list_positions():
                if position.quantity:
                    positions[position.symbol] = position.quantity
        if self._orders is not None:
            for intent in self._orders.list_intents():
                result = self._orders.get_result(intent.internal_order_id)
                if result is None:
                    # Intent never submitted/resulted: treat as expected-open.
                    open_orders[intent.internal_order_id] = intent.symbol
                    continue
                if result.status in _OPEN_STATUSES:
                    open_orders[result.internal_order_id] = result.symbol
                if result.status in (
                    OrderStatus.FILLED,
                    OrderStatus.PARTIALLY_FILLED,
                ):
                    filled[result.internal_order_id] = result.filled_quantity
        return {
            "expected_positions": positions,
            "expected_open_orders": open_orders,
            "expected_filled": filled,
        }

    def actual_from_broker(self) -> dict[str, Any]:
        """Fetch the broker's actual state as domain objects."""
        positions = self._adapter.get_positions()
        orders: list[OrderResult] = []
        if self._orders is not None:
            refs: list[str] = [
                intent.internal_order_id for intent in self._orders.list_intents()
            ]
        else:
            refs = []
        seen: set[str] = set()
        for ref in refs:
            record = self._adapter.get_order_status(ref)
            if record is not None and record.order_id not in seen:
                seen.add(record.order_id)
                orders.append(record_to_result(record))
        return {"actual_positions": positions, "actual_orders": orders}

    def reconcile_now(
        self,
        run_id: str,
        *,
        as_of: datetime | None = None,
        expected: Mapping[str, Any] | None = None,
        actual: dict[str, Any] | None = None,
    ) -> ReconciliationResult:
        """Detect mismatches between expected (local) and actual (broker) state."""
        exp = (
            dict(expected) if expected is not None else dict(self.expected_from_local())
        )
        act = actual if actual is not None else self.actual_from_broker()
        recon_input = ReconciliationInput(
            run_id=run_id,
            as_of=as_of or datetime.now(UTC),
            expected_positions=exp.get("expected_positions", {}),
            expected_open_orders=exp.get("expected_open_orders", {}),
            expected_filled=exp.get("expected_filled", {}),
            actual_positions=act.get("actual_positions"),
            actual_orders=act.get("actual_orders"),
        )
        result = self._engine.reconcile(recon_input)
        if self._recon_repo is not None:
            self._engine.persist(result, self._recon_repo)
        return result

    def end_of_day(
        self,
        run_id: str,
        *,
        as_of: datetime | None = None,
        expected: Mapping[str, Any] | None = None,
        actual: dict[str, Any] | None = None,
    ) -> ReconciliationResult:
        """Mandatory end-of-day reconciliation.

        Identical mechanics to :meth:`reconcile_now`; kept as a distinct,
        named step so operational runbooks and tests can assert it ran. The
        result is always persisted when a reconciliation repository is
        configured, and :meth:`risk_decision` maps a mismatch to a
        LOCK_ACCOUNT risk state.
        """
        return self.reconcile_now(run_id, as_of=as_of, expected=expected, actual=actual)

    def risk_decision(self, result: ReconciliationResult) -> RiskDecision:
        """Map a reconciliation outcome to its risk-kill decision."""
        return self._engine.risk_decision(result)
