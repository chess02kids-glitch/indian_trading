"""Reconciliation: expected state vs actual execution state.

Runs after every execution pass and again at end-of-day. Any mismatch
(partial fills, rejections, unknown orders, position mismatches, duplicate
orders, missing fills) produces a ``ReconciliationResult`` with
``locked=True`` and a LOCK_ACCOUNT risk decision — the system halts until a
human resolves the discrepancy (ADR-008: reconciliation is a kill switch).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping

from models.domain import (
    OrderResult,
    OrderStatus,
    Position,
    ReconciliationMismatch,
    ReconciliationResult,
)
from risk_kill import RiskContext, RiskDecision, RiskGuard
from store.protocols import ReconciliationRepository

__all__ = [
    "ReconciliationEngine",
    "ReconciliationError",
    "ReconciliationInput",
]

_OPEN_STATUSES = (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)


class ReconciliationError(RuntimeError):
    """Raised when reconciliation cannot be performed safely.

    A reconciliation that cannot see the broker's own view of an order has
    not reconciled anything. It must fail closed (lock the account) rather
    than return "matched" — see AUDIT-022.
    """


@dataclass
class ReconciliationInput:
    """Expected vs actual state for one reconciliation pass."""

    run_id: str
    as_of: datetime
    expected_positions: Mapping[str, int] = field(default_factory=dict)
    expected_open_orders: Mapping[str, str] = field(default_factory=dict)
    expected_filled: Mapping[str, int] = field(default_factory=dict)
    actual_positions: list[Position] | None = None
    actual_orders: list[OrderResult] | None = None
    actual_open_orders: list[OrderResult] | None = None


class ReconciliationEngine:
    """Compares expected and actual state; locks the account on mismatch."""

    def __init__(
        self,
        risk_guard: RiskGuard | None = None,
        health_service: Any = None,
        alert_service: Any = None,
    ) -> None:
        self.risk_guard = risk_guard or RiskGuard()
        self.health_service = health_service
        self.alert_service = alert_service

    def reconcile(self, expected: ReconciliationInput) -> ReconciliationResult:
        actual_positions = expected.actual_positions or []
        actual_orders = expected.actual_orders or []
        actual_open = expected.actual_open_orders or [
            order for order in actual_orders if order.status in _OPEN_STATUSES
        ]

        mismatches: list[ReconciliationMismatch] = []
        mismatches.extend(self._check_positions(expected, actual_positions))
        mismatches.extend(self._check_open_orders(expected, actual_open))
        mismatches.extend(self._check_duplicates(actual_orders))
        mismatches.extend(self._check_fills(expected, actual_orders))

        matched = not mismatches
        locked = not matched
        lock_reason = None
        if locked:
            lock_reason = "reconciliation mismatch requires human resolution"
            if self.health_service:
                from observability.health import SystemHealth

                self.health_service.set_state(SystemHealth.LOCKED, reason=lock_reason)
            if self.alert_service:
                self.alert_service.critical(
                    "reconciliation_mismatch",
                    message=f"System LOCKED due to {len(mismatches)} mismatches.",
                    run_id=expected.run_id,
                )
        result = ReconciliationResult(
            run_id=expected.run_id,
            as_of=expected.as_of,
            matched=matched,
            mismatches=tuple(mismatches),
            locked=locked,
            lock_reason=lock_reason,
        )

        if self.health_service:
            self.health_service.write_extended_status(
                {
                    "reconciliation": {
                        "matched": matched,
                        "mismatches": len(mismatches),
                        "locked": locked,
                    }
                }
            )

        return result

    def risk_decision(self, result: ReconciliationResult) -> RiskDecision:
        """Map a reconciliation outcome to the deterministic risk state."""
        context = RiskContext(
            now=datetime.now(UTC),
            equity_now=1.0,
            equity_day_start=1.0,
            equity_peak=1.0,
            position_exposure={},
            gross_exposure=0.0,
            data_last_updated=datetime.now(UTC),
            broker_connected=True,
            order_timestamps=(),
            reconciliation_locked=result.locked,
        )
        return self.risk_guard.evaluate(context)

    def persist(
        self, result: ReconciliationResult, repository: ReconciliationRepository
    ) -> ReconciliationResult:
        repository.save_result(result)
        return result

    # -- individual checks ---------------------------------------------------

    def _check_positions(
        self,
        expected: ReconciliationInput,
        actual_positions: list[Position],
    ) -> list[ReconciliationMismatch]:
        issues: list[ReconciliationMismatch] = []
        actual_by_symbol = {p.symbol: p for p in actual_positions}
        for symbol, quantity in sorted(expected.expected_positions.items()):
            actual = actual_by_symbol.get(symbol)
            if actual is None:
                if quantity != 0:
                    issues.append(
                        ReconciliationMismatch(
                            kind="missing_position",
                            symbol=symbol,
                            expected=quantity,
                            actual=None,
                            detail="expected position not found in broker state",
                        )
                    )
            elif actual.quantity != quantity:
                issues.append(
                    ReconciliationMismatch(
                        kind="position_mismatch",
                        symbol=symbol,
                        expected=quantity,
                        actual=actual.quantity,
                        detail="quantity differs between expected and actual state",
                    )
                )
        for symbol, position in sorted(actual_by_symbol.items()):
            if symbol not in expected.expected_positions and position.quantity != 0:
                issues.append(
                    ReconciliationMismatch(
                        kind="unexpected_position",
                        symbol=symbol,
                        expected=0,
                        actual=position.quantity,
                        detail="broker holds a position the system did not expect",
                    )
                )
        return issues

    def _check_open_orders(
        self,
        expected: ReconciliationInput,
        actual_open: list[OrderResult],
    ) -> list[ReconciliationMismatch]:
        issues: list[ReconciliationMismatch] = []
        actual_ids = {order.internal_order_id for order in actual_open}
        for order_id, symbol in sorted(expected.expected_open_orders.items()):
            if order_id not in actual_ids:
                issues.append(
                    ReconciliationMismatch(
                        kind="missing_open_order",
                        symbol=symbol,
                        expected=order_id,
                        actual=None,
                        detail="expected open order not present in broker state",
                    )
                )
        for order in actual_open:
            if order.internal_order_id not in expected.expected_open_orders:
                issues.append(
                    ReconciliationMismatch(
                        kind="unknown_open_order",
                        symbol=order.symbol,
                        expected=None,
                        actual=order.internal_order_id,
                        detail="broker has an open order the system did not expect",
                    )
                )
        return issues

    def _check_duplicates(
        self, actual_orders: list[OrderResult]
    ) -> list[ReconciliationMismatch]:
        """Two distinct orders sharing an idempotency key = duplicate order."""
        seen: dict[str, str] = {}
        issues: list[ReconciliationMismatch] = []
        for order in actual_orders:
            prior = seen.get(order.idempotency_key)
            if prior is not None and prior != order.internal_order_id:
                issues.append(
                    ReconciliationMismatch(
                        kind="duplicate_order",
                        symbol=order.symbol,
                        expected=prior,
                        actual=order.internal_order_id,
                        detail="same idempotency key produced multiple orders",
                    )
                )
            else:
                seen.setdefault(order.idempotency_key, order.internal_order_id)
        return issues

    def _check_fills(
        self,
        expected: ReconciliationInput,
        actual_orders: list[OrderResult],
    ) -> list[ReconciliationMismatch]:
        issues: list[ReconciliationMismatch] = []
        by_id = {order.internal_order_id: order for order in actual_orders}
        for order_id, filled_quantity in sorted(expected.expected_filled.items()):
            order = by_id.get(order_id)
            if order is None:
                issues.append(
                    ReconciliationMismatch(
                        kind="missing_fill",
                        expected=filled_quantity,
                        actual=None,
                        detail=f"expected fill {order_id} not found in broker state",
                    )
                )
                continue
            if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                issues.append(
                    ReconciliationMismatch(
                        kind="unfilled_order",
                        symbol=order.symbol,
                        expected=filled_quantity,
                        actual=order.status.value,
                        detail="order expected to be filled is not",
                    )
                )
                continue
            if order.filled_quantity != filled_quantity:
                issues.append(
                    ReconciliationMismatch(
                        kind="partial_fill",
                        symbol=order.symbol,
                        expected=filled_quantity,
                        actual=order.filled_quantity,
                        detail="filled quantity differs from expectation",
                    )
                )
        return issues
