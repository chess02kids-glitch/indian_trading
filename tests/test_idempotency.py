"""Phase 1 tests: deterministic idempotency and duplicate rejection."""

from __future__ import annotations

from execution.idempotency import (
    IdempotencyRegistry,
    compute_idempotency_key,
)


def _fields(**overrides) -> dict:
    base = {
        "strategy_id": "momentum-quality",
        "hypothesis_id": "HYP-00001",
        "symbol": "reliance",
        "side": "buy",
        "quantity": 10,
        "limit_price": 1250.5,
        "order_type": "limit",
        "rebalance_date": "2026-08-24",
    }
    base.update(overrides)
    return base


class TestKeyGeneration:
    def test_deterministic(self) -> None:
        assert compute_idempotency_key(_fields()) == compute_idempotency_key(_fields())

    def test_field_order_independent(self) -> None:
        a = compute_idempotency_key(_fields())
        b = compute_idempotency_key(
            {k: _fields()[k] for k in reversed(list(_fields()))}
        )
        assert a == b

    def test_logically_identical_orders_share_key(self) -> None:
        # Case/whitespace differences in identifiers must not change the key.
        first = compute_idempotency_key(_fields())
        retry = compute_idempotency_key(
            _fields(symbol="RELIANCE", side="BUY", order_type="LIMIT")
        )
        assert first == retry

    def test_different_logical_orders_get_different_keys(self) -> None:
        base = compute_idempotency_key(_fields())
        assert compute_idempotency_key(_fields(quantity=11)) != base
        assert compute_idempotency_key(_fields(limit_price=1251.0)) != base
        assert compute_idempotency_key(_fields(side="sell")) != base
        assert compute_idempotency_key(_fields(rebalance_date="2026-09-24")) != base
        assert compute_idempotency_key(_fields(symbol="TCS")) != base

    def test_missing_fields_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="missing"):
            compute_idempotency_key({"symbol": "RELIANCE"})


class TestRegistry:
    def test_request_timeout_retry_same_key_duplicate_rejected(self) -> None:
        """Final suite (test 4): request -> timeout -> retry -> same key ->
        duplicate rejected, no second order."""
        registry = IdempotencyRegistry()
        key = compute_idempotency_key(_fields())

        first = registry.claim(key)
        assert first.accepted is True

        # Simulated timeout: the order is still in flight.
        retry = registry.claim(key)
        assert retry.accepted is False
        assert retry.reason == "in flight"

        # After completion, a retry is still a duplicate.
        registry.mark_completed(key)
        after_completion = registry.claim(key)
        assert after_completion.accepted is False
        assert after_completion.reason == "already completed"

    def test_distinct_orders_both_accepted(self) -> None:
        registry = IdempotencyRegistry()
        assert registry.claim(compute_idempotency_key(_fields())).accepted
        assert registry.claim(compute_idempotency_key(_fields(symbol="TCS"))).accepted

    def test_empty_key_rejected(self) -> None:
        registry = IdempotencyRegistry()
        assert registry.claim("").accepted is False
        assert registry.claim("   ").accepted is False

    def test_accepted_keys_snapshot_for_risk_guard(self) -> None:
        registry = IdempotencyRegistry()
        key = compute_idempotency_key(_fields())
        registry.claim(key)
        registry.mark_completed(key)
        snapshot = registry.accepted_keys()
        assert snapshot == {key: True}
