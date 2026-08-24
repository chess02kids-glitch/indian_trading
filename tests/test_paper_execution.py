"""Phase 3 / final-suite tests for the deterministic paper broker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from execution.idempotency import compute_idempotency_key
from execution.paper import PaperBroker, PaperBrokerConfig
from execution.validation import OrderValidationError
from models.domain import OrderIntent, OrderSide, OrderStatus, OrderType

T0 = datetime(2026, 8, 24, 9, 15, tzinfo=UTC)


def _intent(
    order_id: str = "ord-1",
    key: str | None = None,
    symbol: str = "RELIANCE",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    price: float = 100.0,
    ts: datetime | None = None,
) -> OrderIntent:
    if key is None:
        key = compute_idempotency_key(
            {
                "strategy_id": "s",
                "hypothesis_id": "h",
                "symbol": symbol,
                "side": side.value,
                "quantity": quantity,
                "limit_price": price,
                "order_type": "limit",
                "rebalance_date": "2026-08-24",
            }
        )
    return OrderIntent.model_validate(
        {
            "internal_order_id": order_id,
            "idempotency_key": key,
            "strategy_id": "s",
            "hypothesis_id": "h",
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "limit_price": price,
            "order_type": OrderType.LIMIT,
            "timestamp": ts or T0,
        }
    )


def _broker(**config) -> PaperBroker:
    defaults = dict(
        seed=7,
        fill_probability=1.0,
        partial_fill_probability=0.0,  # deterministic full fills by default
        partial_fill_fraction=0.5,
    )
    defaults.update(config)
    return PaperBroker(T0, PaperBrokerConfig(**defaults))


class TestFills:
    def test_full_fill_updates_position_and_cash(self) -> None:
        broker = _broker()
        result = broker.submit_order(
            _intent(quantity=10, price=100.0), reference_price=100.0
        )
        assert result.status is OrderStatus.FILLED
        assert result.filled_quantity == 10
        position = broker.get_positions()[0]
        assert position.quantity == 10
        assert position.average_price == 100.0
        assert broker.get_cash() == 1_000_000.0 - 1000.0

    def test_partial_fill_updates_position(self) -> None:
        """Final suite (test 8): a partial fill updates the position."""
        broker = _broker(partial_fill_probability=1.0, partial_fill_fraction=0.5)
        result = broker.submit_order(
            _intent(quantity=10, price=100.0), reference_price=100.0
        )
        assert result.status is OrderStatus.PARTIALLY_FILLED
        assert result.filled_quantity == 5
        position = broker.get_positions()[0]
        assert position.quantity == 5
        assert broker.get_cash() == 1_000_000.0 - 500.0

    def test_partial_fill_deterministic_with_seed(self) -> None:
        first = _broker(partial_fill_probability=0.5, partial_fill_fraction=0.25)
        second = _broker(partial_fill_probability=0.5, partial_fill_fraction=0.25)
        r1 = first.submit_order(_intent(quantity=11, price=50.0), reference_price=50.0)
        r2 = second.submit_order(_intent(quantity=11, price=50.0), reference_price=50.0)
        assert r1.status == r2.status
        assert r1.filled_quantity == r2.filled_quantity

    def test_default_partial_fill_invariant(self) -> None:
        # With the default 20% partial probability, whatever the seed draws,
        # the position and cash must always reflect the actual fill.
        broker = _broker(seed=11, partial_fill_probability=0.2)
        for i in range(12):
            intent = _intent(order_id=f"ord-{i}", quantity=9, price=20.0)
            result = broker.submit_order(intent, reference_price=20.0)
            if result.status is OrderStatus.PARTIALLY_FILLED:
                assert 0 < result.filled_quantity < 9
        total_filled = sum(p.quantity for p in broker.get_positions())
        expected_cash = 1_000_000.0 - total_filled * 20.0
        assert broker.get_cash() == expected_cash

    def test_sell_requires_position(self) -> None:
        broker = _broker()
        result = broker.submit_order(
            _intent(order_id="ord-2", symbol="RELIANCE", side=OrderSide.SELL),
            reference_price=100.0,
        )
        assert result.status is OrderStatus.REJECTED
        assert "insufficient position" in (result.reason or "")

    def test_buy_requires_cash(self) -> None:
        broker = _broker(initial_cash=500.0)
        result = broker.submit_order(
            _intent(quantity=10, price=100.0), reference_price=100.0
        )
        assert result.status is OrderStatus.REJECTED
        assert "insufficient cash" in (result.reason or "")

    def test_price_band_violation_rejected_explicitly(self) -> None:
        broker = _broker()
        with pytest.raises(OrderValidationError, match="deviates"):
            broker.submit_order(_intent(price=200.0), reference_price=100.0)

    def test_market_order_never_reaches_broker(self) -> None:
        broker = _broker()
        bad = _intent(order_id="ord-3")
        bad_dict = bad.model_dump(mode="json")
        bad_dict["order_type"] = "MARKET"
        bad_dict["internal_order_id"] = "ord-market"
        with pytest.raises(Exception):
            broker.submit_order(
                OrderIntent.model_validate(bad_dict), reference_price=100.0
            )
        assert broker.get_order_status("ord-market") is None


class TestPendingCancelTimeout:
    def test_pending_order_expires_after_ttl(self) -> None:
        broker = _broker(fill_probability=0.0, order_ttl_days=2.0)
        result = broker.submit_order(
            _intent(quantity=5, price=10.0), reference_price=10.0
        )
        assert result.status is OrderStatus.PENDING
        assert len(broker.get_open_orders()) == 1
        broker.advance_clock(T0 + timedelta(days=3))
        updated = broker.get_order_status("ord-1")
        assert updated is not None
        assert updated.status is OrderStatus.EXPIRED
        assert broker.get_open_orders() == []

    def test_cancel_pending_order(self) -> None:
        broker = _broker(fill_probability=0.0)
        broker.submit_order(_intent(quantity=5, price=10.0), reference_price=10.0)
        cancelled = broker.cancel_order("ord-1")
        assert cancelled is not None
        assert cancelled.status is OrderStatus.CANCELLED
        assert broker.get_open_orders() == []

    def test_cancel_terminal_order_is_noop(self) -> None:
        broker = _broker()
        broker.submit_order(_intent(quantity=5, price=10.0), reference_price=10.0)
        result = broker.cancel_order("ord-1")
        assert result is not None
        assert result.status is OrderStatus.FILLED

    def test_clock_cannot_move_backwards(self) -> None:
        broker = _broker()
        with pytest.raises(ValueError):
            broker.advance_clock(T0 - timedelta(hours=1))


class TestDuplicates:
    def test_duplicate_request_does_not_double_fill(self) -> None:
        """Final suite (test 4) at the broker level: duplicate request ->
        same idempotency key -> no second order."""
        broker = _broker()
        intent = _intent(quantity=10, price=100.0)
        first = broker.submit_order(intent, reference_price=100.0)
        second = broker.submit_order(intent, reference_price=100.0)
        assert second.internal_order_id == first.internal_order_id
        assert second.status == first.status
        assert broker.get_cash() == 1_000_000.0 - 1000.0  # filled once
        assert broker.get_open_orders() == []
        positions = broker.get_positions()
        assert sum(p.quantity for p in positions) == 10

    def test_duplicate_pending_returns_original(self) -> None:
        broker = _broker(fill_probability=0.0)
        intent = _intent(quantity=5, price=10.0)
        first = broker.submit_order(intent, reference_price=10.0)
        second = broker.submit_order(intent, reference_price=10.0)
        assert second.status is OrderStatus.PENDING
        assert second.internal_order_id == first.internal_order_id
        assert len(broker._orders) == 1

    def test_distinct_intents_both_execute(self) -> None:
        broker = _broker()
        a = broker.submit_order(
            _intent(order_id="ord-a", symbol="RELIANCE"), reference_price=100.0
        )
        b = broker.submit_order(
            _intent(order_id="ord-b", symbol="TCS", price=400.0), reference_price=400.0
        )
        assert a.status is OrderStatus.FILLED
        assert b.status is OrderStatus.FILLED
        assert len(broker.get_positions()) == 2


class TestStateSnapshot:
    def test_get_state_shape(self) -> None:
        broker = _broker()
        broker.submit_order(_intent(quantity=2, price=50.0), reference_price=50.0)
        state = broker.get_state()
        assert state["cash"] == 1_000_000.0 - 100.0
        assert state["positions"][0]["symbol"] == "RELIANCE"
        assert state["orders_total"] == 1
