"""Phase 1 / final-suite tests for the LIMIT-only order invariant."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from execution.validation import (
    ExecutionBlockedError,
    OrderValidationError,
    validate_limit_price_band,
    validate_order_intent,
    validate_order_intents,
)
from models.domain import (
    ExecutionMode,
    OrderIntent,
    OrderSide,
    OrderType,
)

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _intent(**overrides) -> dict:
    base = {
        "internal_order_id": "ord-0001",
        "idempotency_key": "k-0001",
        "strategy_id": "momentum-quality",
        "hypothesis_id": "HYP-00001",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 1250.5,
        "order_type": OrderType.LIMIT,
        "timestamp": NOW,
        "target_position": 10,
    }
    base.update(overrides)
    return base


def _make(**overrides) -> OrderIntent:
    return OrderIntent.model_validate(_intent(**overrides))


class TestLimitOnlyInvariant:
    """Final suite: MARKET (test 2) and IOC (test 3) orders are rejected."""

    def test_market_order_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            _make(order_type="MARKET")

    def test_ioc_order_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            _make(order_type="IOC")

    def test_limit_with_market_rejected_at_construction(self) -> None:
        with pytest.raises(ValidationError):
            _make(order_type="LIMIT_WITH_MARKET")

    def test_market_order_rejected_by_validation_point(self) -> None:
        # A dict bypasses construction; the validation point must still reject.
        with pytest.raises(OrderValidationError, match="LIMIT"):
            validate_order_intent(_intent(order_type="MARKET"))

    def test_ioc_order_rejected_by_validation_point(self) -> None:
        with pytest.raises(OrderValidationError, match="LIMIT"):
            validate_order_intent(_intent(order_type="IOC"))

    def test_order_type_enum_has_exactly_one_member(self) -> None:
        assert [member for member in OrderType] == [OrderType.LIMIT]

    def test_valid_limit_order_passes(self) -> None:
        validated = validate_order_intent(_make(), now=NOW)
        assert validated.order_type is OrderType.LIMIT

    def test_invalid_input_is_never_converted_to_limit(self) -> None:
        # Rejection must be explicit, not a silent normalization. The dict
        # form exercises the validation point directly (construction of a
        # MARKET order is itself impossible).
        with pytest.raises(OrderValidationError, match="invalid"):
            validate_order_intent(_intent(order_type="MARKET"))


class TestOrderFieldValidation:
    def test_zero_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(quantity=0)

    def test_negative_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(quantity=-5)

    def test_fractional_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(quantity=10.5)

    def test_non_numeric_quantity_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(quantity="ten")

    def test_missing_limit_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(limit_price=None)

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(limit_price=-1)

    def test_nan_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(limit_price=float("nan"))

    def test_inf_price_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(limit_price=float("inf"))

    def test_invalid_symbol_rejected(self) -> None:
        for bad in ("", "REL IANCE", "1!BAD", "x" * 30):
            with pytest.raises(ValidationError):
                _make(symbol=bad)

    def test_lowercase_symbol_normalized(self) -> None:
        assert _make(symbol="reliance").symbol == "RELIANCE"

    def test_invalid_exchange_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(exchange="NYSE")

    def test_invalid_side_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _make(side="LONG")

    def test_missing_identifiers_rejected(self) -> None:
        for field_name in (
            "internal_order_id",
            "idempotency_key",
            "strategy_id",
            "hypothesis_id",
        ):
            with pytest.raises(ValidationError):
                _make(**{field_name: ""})

    def test_future_timestamp_rejected(self) -> None:
        future = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
        intent = _make(timestamp=future)
        with pytest.raises(OrderValidationError, match="future"):
            validate_order_intent(intent, now=NOW)

    def test_batch_validation_fails_atomically(self) -> None:
        batch = [_intent(), _intent(order_type="MARKET")]
        with pytest.raises(OrderValidationError):
            validate_order_intents(batch)


class TestPriceBandAndMode:
    def test_price_outside_band_rejected(self) -> None:
        intent = _make(limit_price=999.0)
        with pytest.raises(OrderValidationError, match="deviates"):
            validate_limit_price_band(intent, reference_price=1250.0, band_fraction=0.1)

    def test_price_inside_band_accepted(self) -> None:
        intent = _make(limit_price=1260.0)
        validate_limit_price_band(intent, reference_price=1250.0, band_fraction=0.1)

    def test_bad_reference_price_rejected(self) -> None:
        intent = _make()
        for bad in (0, -5, float("nan"), None):
            with pytest.raises(OrderValidationError):
                validate_limit_price_band(intent, bad, band_fraction=0.1)

    def test_only_research_paper_sandbox_modes_permitted(self) -> None:
        from execution.validation import validate_execution_mode

        for mode in ("RESEARCH", "PAPER", "SANDBOX", "paper"):
            assert validate_execution_mode(mode) in ExecutionMode

        with pytest.raises((OrderValidationError, ExecutionBlockedError)):
            validate_execution_mode("LIVE")


class TestExecutionModeEnum:
    def test_no_live_member(self) -> None:
        names = {member.name for member in ExecutionMode}
        assert "LIVE" not in names
        assert names == {"RESEARCH", "PAPER", "SANDBOX"}
