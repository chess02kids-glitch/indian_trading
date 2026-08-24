"""Phase 3 / final-suite tests for reconciliation (test 9: mismatch locks)."""

from __future__ import annotations

from datetime import UTC, datetime

from models.domain import OrderResult, OrderStatus, Position
from reconciliation.engine import ReconciliationEngine, ReconciliationInput
from risk_kill import RiskState

T0 = datetime(2026, 8, 24, 18, 0, tzinfo=UTC)


def _positions() -> list[Position]:
    return [
        Position(symbol="RELIANCE", quantity=10, average_price=100.0),
        Position(symbol="TCS", quantity=5, average_price=400.0),
    ]


def _engine() -> ReconciliationEngine:
    return ReconciliationEngine()


class TestCleanReconciliation:
    def test_matching_state_matches(self) -> None:
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_positions={"RELIANCE": 10, "TCS": 5},
            actual_positions=_positions(),
        )
        result = _engine().reconcile(expected)
        assert result.matched is True
        assert result.locked is False
        assert result.mismatches == ()

    def test_decision_is_nominal_when_matched(self) -> None:
        result = _engine().reconcile(ReconciliationInput(run_id="r", as_of=T0))
        decision = _engine().risk_decision(result)
        assert decision.state is RiskState.NOMINAL


class TestMismatchLocks:
    def test_final_suite_position_mismatch_locks_account(self) -> None:
        """Final suite (test 9): reconciliation mismatch locks the account."""
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_positions={"RELIANCE": 10},
            actual_positions=[
                Position(symbol="RELIANCE", quantity=7, average_price=100.0)
            ],
        )
        result = _engine().reconcile(expected)
        assert result.matched is False
        assert result.locked is True
        kinds = {m.kind for m in result.mismatches}
        assert "position_mismatch" in kinds
        decision = _engine().risk_decision(result)
        assert decision.state is RiskState.LOCK_ACCOUNT
        assert decision.human_action_required is True

    def test_unexpected_position_locks(self) -> None:
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_positions={},
            actual_positions=[Position(symbol="WIPRO", quantity=3)],
        )
        result = _engine().reconcile(expected)
        assert result.locked is True
        assert {m.kind for m in result.mismatches} >= {"unexpected_position"}

    def test_unknown_open_order_locks(self) -> None:
        order = OrderResult.model_validate(
            {
                "internal_order_id": "ord-x",
                "idempotency_key": "k-x",
                "symbol": "TCS",
                "status": OrderStatus.PENDING,
                "requested_quantity": 1,
                "timestamp": T0,
            }
        )
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_open_orders={},
            actual_open_orders=[order],
        )
        result = _engine().reconcile(expected)
        assert result.locked is True
        assert {m.kind for m in result.mismatches} >= {"unknown_open_order"}

    def test_missing_open_order_locks(self) -> None:
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_open_orders={"ord-1": "RELIANCE"},
            actual_open_orders=[],
        )
        result = _engine().reconcile(expected)
        assert result.locked is True
        assert {m.kind for m in result.mismatches} >= {"missing_open_order"}

    def test_duplicate_order_locks(self) -> None:
        def order(order_id: str, key: str) -> OrderResult:
            return OrderResult.model_validate(
                {
                    "internal_order_id": order_id,
                    "idempotency_key": key,
                    "symbol": "RELIANCE",
                    "status": OrderStatus.FILLED,
                    "requested_quantity": 1,
                    "filled_quantity": 1,
                    "average_fill_price": 100.0,
                    "timestamp": T0,
                }
            )

        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            actual_orders=[order("ord-1", "key-1"), order("ord-2", "key-1")],
        )
        result = _engine().reconcile(expected)
        assert result.locked is True
        assert {m.kind for m in result.mismatches} >= {"duplicate_order"}

    def test_missing_fill_locks(self) -> None:
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_filled={"ord-1": 10},
            actual_orders=[],
        )
        result = _engine().reconcile(expected)
        assert result.locked is True
        assert {m.kind for m in result.mismatches} >= {"missing_fill"}

    def test_partial_fill_differs_from_expectation(self) -> None:
        order = OrderResult.model_validate(
            {
                "internal_order_id": "ord-1",
                "idempotency_key": "k-1",
                "symbol": "RELIANCE",
                "status": OrderStatus.PARTIALLY_FILLED,
                "requested_quantity": 10,
                "filled_quantity": 4,
                "average_fill_price": 100.0,
                "timestamp": T0,
            }
        )
        expected = ReconciliationInput(
            run_id="run-1",
            as_of=T0,
            expected_filled={"ord-1": 10},
            actual_orders=[order],
        )
        result = _engine().reconcile(expected)
        assert result.locked is True
        assert {m.kind for m in result.mismatches} >= {"partial_fill"}

    def test_rejection_is_not_a_mismatch_when_unexpected(self) -> None:
        """A rejected order the system did not expect to be filled is clean:
        the system tracks its own expectation, so no fill is expected."""
        order = OrderResult.model_validate(
            {
                "internal_order_id": "ord-1",
                "idempotency_key": "k-1",
                "symbol": "RELIANCE",
                "status": OrderStatus.REJECTED,
                "requested_quantity": 10,
                "timestamp": T0,
            }
        )
        result = _engine().reconcile(
            ReconciliationInput(run_id="run-1", as_of=T0, actual_orders=[order])
        )
        assert result.matched is True
