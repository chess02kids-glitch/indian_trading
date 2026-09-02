"""Regressions for AUDIT-025 — the order state machine actually exists.

``execution/state_machine.py`` was a **0-byte file**. The documented order
lifecycle was prose; ``PaperBroker`` mutated ``self._orders[id]`` directly,
so ``FILLED -> PENDING``, ``CANCELLED -> FILLED`` and ``REJECTED -> FILLED``
were all representable and none of them were logged. These tests pin the
machine that now governs every mutation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from execution.paper import PaperBroker
from execution.state_machine import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidOrderTransition,
    OrderStateMachine,
    can_transition,
    is_terminal,
    validate_quantity_invariants,
    validate_result_transition,
    validate_transition,
)
from models.domain import OrderResult, OrderSide, OrderStatus


def _result(
    status: OrderStatus,
    *,
    order_id: str = "ORD-1",
    requested: int = 100,
    filled: int | None = None,
    price: float | None = 100.0,
) -> OrderResult:
    if filled is None:
        filled = {
            OrderStatus.PENDING: 0,
            OrderStatus.PARTIALLY_FILLED: 50,
            OrderStatus.FILLED: 100,
            OrderStatus.CANCELLED: 0,
            OrderStatus.REJECTED: 0,
            OrderStatus.EXPIRED: 0,
            OrderStatus.UNKNOWN: 0,
        }[status]
    return OrderResult(
        internal_order_id=order_id,
        idempotency_key=f"ik-{order_id}",
        broker_order_id=f"brk-{order_id}",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        status=status,
        requested_quantity=requested,
        filled_quantity=filled,
        average_fill_price=price if filled else None,
        timestamp=datetime(2026, 1, 5, 9, 30, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------


class TestLifecycleTable:
    def test_the_file_is_no_longer_empty(self):
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "execution" / "state_machine.py"
        assert path.stat().st_size > 0

    def test_every_status_has_an_entry(self):
        assert set(ALLOWED_TRANSITIONS) == set(OrderStatus)

    def test_terminal_states_accept_nothing(self):
        for status in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[status] == frozenset()
            assert is_terminal(status)

    def test_terminal_states_cannot_reach_any_state(self):
        for terminal in TERMINAL_STATES:
            for target in OrderStatus:
                assert not can_transition(terminal, target)

    @pytest.mark.parametrize(
        "target",
        [
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.UNKNOWN,
        ],
    )
    def test_pending_can_move_to_a_live_state(self, target):
        assert can_transition(OrderStatus.PENDING, target)

    def test_a_filled_order_can_never_be_unfilled(self):
        assert not can_transition(OrderStatus.FILLED, OrderStatus.PENDING)
        assert not can_transition(OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)

    def test_a_cancelled_order_can_never_fill(self):
        assert not can_transition(OrderStatus.CANCELLED, OrderStatus.FILLED)

    def test_a_rejected_order_can_never_fill(self):
        assert not can_transition(OrderStatus.REJECTED, OrderStatus.FILLED)

    def test_an_expired_order_can_never_fill(self):
        assert not can_transition(OrderStatus.EXPIRED, OrderStatus.FILLED)

    def test_unknown_is_resolvable(self):
        for target in OrderStatus:
            if target is not OrderStatus.UNKNOWN:
                assert can_transition(OrderStatus.UNKNOWN, target)

    def test_no_real_state_falls_back_to_unknown(self):
        """Only an explicit 'we lost sight of it' may produce UNKNOWN."""
        for status in OrderStatus:
            if status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
                # allowed: a broker poll may fail to see a live order
                assert can_transition(status, OrderStatus.UNKNOWN)
            else:
                assert not can_transition(status, OrderStatus.UNKNOWN)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class TestValidateTransition:
    def test_creating_an_order_is_always_allowed(self):
        validate_transition(None, OrderStatus.FILLED, order_id="ORD-1")

    def test_illegal_move_raises(self):
        with pytest.raises(InvalidOrderTransition) as excinfo:
            validate_transition(OrderStatus.FILLED, OrderStatus.PENDING, order_id="X")
        assert "cannot move FILLED -> PENDING" in str(excinfo.value)
        assert excinfo.value.order_id == "X"

    def test_the_error_names_terminal_states(self):
        with pytest.raises(InvalidOrderTransition, match="terminal"):
            validate_transition(OrderStatus.EXPIRED, OrderStatus.FILLED)

    def test_result_transition_accepts_a_legal_move(self):
        previous = _result(OrderStatus.PENDING)
        new = _result(OrderStatus.FILLED)
        assert validate_result_transition(previous, new) is new


class TestQuantityInvariants:
    def test_a_fill_may_never_shrink(self):
        previous = _result(OrderStatus.PARTIALLY_FILLED, filled=50)
        new = _result(OrderStatus.PARTIALLY_FILLED, filled=10)
        with pytest.raises(InvalidOrderTransition, match="decreased"):
            validate_quantity_invariants(previous, new)

    def test_a_fill_may_never_exceed_the_request(self):
        new = _result(OrderStatus.FILLED, requested=100, filled=120)
        with pytest.raises(InvalidOrderTransition, match="exceeds"):
            validate_quantity_invariants(None, new)

    def test_filled_requires_the_full_quantity(self):
        new = _result(OrderStatus.FILLED, requested=100, filled=99)
        with pytest.raises(InvalidOrderTransition, match="requires"):
            validate_quantity_invariants(None, new)

    def test_a_fill_needs_a_price(self):
        new = _result(OrderStatus.PARTIALLY_FILLED, filled=50, price=None)
        with pytest.raises(InvalidOrderTransition, match="no positive"):
            validate_quantity_invariants(None, new)

    def test_a_pending_order_may_have_no_price(self):
        validate_quantity_invariants(None, _result(OrderStatus.PENDING, filled=0))


# ---------------------------------------------------------------------------
# The tracker
# ---------------------------------------------------------------------------


class TestOrderStateMachine:
    def test_history_is_recorded(self):
        machine = OrderStateMachine(internal_order_id="ORD-1")
        machine.record(_result(OrderStatus.PENDING), reason="submitted")
        machine.transition(
            _result(OrderStatus.PENDING),
            _result(OrderStatus.FILLED),
            reason="filled",
        )
        assert [status for _, status, _ in machine.history] == [
            OrderStatus.PENDING,
            OrderStatus.FILLED,
        ]
        assert [reason for _, _, reason in machine.history] == [
            "submitted",
            "filled",
        ]

    def test_a_refused_transition_leaves_no_trace(self):
        machine = OrderStateMachine(internal_order_id="ORD-1")
        machine.record(_result(OrderStatus.FILLED))
        with pytest.raises(InvalidOrderTransition):
            machine.transition(
                _result(OrderStatus.FILLED), _result(OrderStatus.PENDING)
            )
        assert len(machine.history) == 1


# ---------------------------------------------------------------------------
# End to end: the broker refuses an illegal move
# ---------------------------------------------------------------------------


def test_broker_refuses_to_unfill_an_order():
    from execution.paper import PaperBrokerConfig
    from models.domain import OrderIntent

    broker = PaperBroker(datetime.now(UTC), PaperBrokerConfig(fill_probability=1.0))
    intent = OrderIntent(
        internal_order_id="ORD-STATE-1",
        idempotency_key="ik-state-1",
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        strategy_id="test-strategy",
        hypothesis_id="HYP-STATE",
        quantity=10,
        limit_price=100.0,
        timestamp=datetime.now(UTC),
    )
    result = broker.submit_order(intent, reference_price=100.0)
    assert result.status is OrderStatus.FILLED

    # A bug (or a hostile adapter) trying to put the order back in the book.
    with pytest.raises(InvalidOrderTransition):
        broker._store(_result(OrderStatus.PENDING, order_id="ORD-STATE-1"))

    # The ledger is untouched — the fill stands.
    assert broker.get_order_status("ORD-STATE-1").status is OrderStatus.FILLED


def test_broker_refuses_to_fill_a_cancelled_order():
    from execution.paper import PaperBrokerConfig
    from models.domain import OrderIntent

    broker = PaperBroker(datetime.now(UTC), PaperBrokerConfig(fill_probability=0.0))
    intent = OrderIntent(
        internal_order_id="ORD-STATE-2",
        idempotency_key="ik-state-2",
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        strategy_id="test-strategy",
        hypothesis_id="HYP-STATE",
        quantity=10,
        limit_price=100.0,
        timestamp=datetime.now(UTC),
    )
    pending = broker.submit_order(intent, reference_price=100.0)
    assert pending.status is OrderStatus.PENDING
    cancelled = broker.cancel_order("ORD-STATE-2")
    assert cancelled.status is OrderStatus.CANCELLED

    with pytest.raises(InvalidOrderTransition):
        broker._store(_result(OrderStatus.FILLED, order_id="ORD-STATE-2"))
    assert broker.get_order_status("ORD-STATE-2").status is OrderStatus.CANCELLED


def test_broker_records_the_transition_history():
    from execution.paper import PaperBrokerConfig
    from models.domain import OrderIntent

    broker = PaperBroker(datetime.now(UTC), PaperBrokerConfig(fill_probability=1.0))
    intent = OrderIntent(
        internal_order_id="ORD-STATE-3",
        idempotency_key="ik-state-3",
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        strategy_id="test-strategy",
        hypothesis_id="HYP-STATE",
        quantity=10,
        limit_price=100.0,
        timestamp=datetime.now(UTC),
    )
    broker.submit_order(intent, reference_price=100.0)
    machine = broker._machines["ORD-STATE-3"]
    assert [status for _, status, _ in machine.history] == [OrderStatus.FILLED]
