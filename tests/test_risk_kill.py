"""Phase 1 / final-suite tests for the deterministic risk-kill guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from risk_kill import (
    RISK_RESPONSES,
    RiskCheckError,
    RiskContext,
    RiskGuard,
    RiskLimits,
    RiskState,
    worst_state,
)

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)

HEALTHY = dict(
    equity_now=1_000_000.0,
    equity_day_start=1_000_000.0,
    equity_peak=1_000_000.0,
    position_exposure={},
    gross_exposure=0.0,
    data_last_updated=NOW - timedelta(hours=1),
    broker_connected=True,
    order_timestamps=(),
    reconciliation_locked=False,
)


def _guard(**limits) -> RiskGuard:
    return RiskGuard(RiskLimits(**limits))


def _context(**overrides) -> RiskContext:
    values = dict(HEALTHY)
    values.update(overrides)
    return RiskContext(now=NOW, **values)


class TestBaseline:
    def test_healthy_context_is_nominal(self) -> None:
        decision = _guard().evaluate(_context())
        assert decision.state is RiskState.NOMINAL
        assert decision.triggered_by == ()
        assert decision.human_action_required is False

    def test_five_protective_states_exist(self) -> None:
        assert {state.value for state in RISK_RESPONSES} == {
            "STOP_NEW_ORDERS",
            "CANCEL_OPEN_ORDERS",
            "FLATTEN_POSITIONS",
            "LOCK_ACCOUNT",
            "ALERT_HUMAN",
        }

    def test_worst_state_aggregation(self) -> None:
        assert worst_state(
            RiskState.STOP_NEW_ORDERS, RiskState.CANCEL_OPEN_ORDERS
        ) is RiskState.CANCEL_OPEN_ORDERS
        assert worst_state(None, None) is RiskState.NOMINAL


class TestDailyLoss:
    def test_within_limit_is_nominal(self) -> None:
        decision = _guard(max_daily_loss=0.03).evaluate(
            _context(equity_now=985_000.0)
        )
        assert decision.state is RiskState.NOMINAL

    def test_daily_loss_triggers_stop(self) -> None:
        """Final suite (test 5): daily loss triggers STOP_NEW_ORDERS."""
        decision = _guard(max_daily_loss=0.03).evaluate(
            _context(equity_now=965_000.0)
        )
        assert decision.state is RiskState.STOP_NEW_ORDERS
        assert "daily_loss" in decision.triggered_by
        assert decision.human_action_required is True

    def test_unknown_equity_fails_closed(self) -> None:
        decision = _guard().evaluate(_context(equity_now=None))
        assert decision.state is RiskState.LOCK_ACCOUNT


class TestDrawdown:
    def test_drawdown_triggers_flatten(self) -> None:
        """Final suite (test 6): max drawdown triggers FLATTEN_POSITIONS."""
        decision = _guard(max_drawdown=0.10).evaluate(
            _context(equity_peak=1_000_000.0, equity_now=890_000.0)
        )
        assert decision.state is RiskState.FLATTEN_POSITIONS
        assert "max_drawdown" in decision.triggered_by

    def test_drawdown_within_limit_is_nominal(self) -> None:
        decision = _guard(max_drawdown=0.10).evaluate(
            _context(
                equity_peak=1_000_000.0,
                equity_now=950_000.0,
                equity_day_start=950_000.0,  # no same-day loss
            )
        )
        assert decision.state is RiskState.NOMINAL


class TestExposure:
    def test_position_exposure_triggers_cancel_open(self) -> None:
        decision = _guard(max_position_exposure=0.25).evaluate(
            _context(
                position_exposure={"RELIANCE": 300_000.0}, gross_exposure=300_000.0
            )
        )
        assert decision.state is RiskState.CANCEL_OPEN_ORDERS

    def test_gross_exposure_triggers_flatten(self) -> None:
        decision = _guard(max_gross_exposure=1.0).evaluate(
            _context(
                position_exposure={"A": 500_000.0, "B": 600_000.0},
                gross_exposure=1_100_000.0,
            )
        )
        assert decision.state is RiskState.FLATTEN_POSITIONS

    def test_unknown_exposure_fails_closed(self) -> None:
        decision = _guard().evaluate(_context(position_exposure=None))
        assert decision.state is RiskState.LOCK_ACCOUNT


class TestStaleness:
    def test_stale_data_stops_new_orders(self) -> None:
        decision = _guard(max_data_age_hours=18).evaluate(
            _context(data_last_updated=NOW - timedelta(hours=20))
        )
        assert decision.state is RiskState.STOP_NEW_ORDERS
        assert "data_staleness" in decision.triggered_by

    def test_fresh_data_is_nominal(self) -> None:
        assert _guard().evaluate(_context()).state is RiskState.NOMINAL

    def test_missing_data_timestamp_fails_closed(self) -> None:
        decision = _guard().evaluate(_context(data_last_updated=None))
        assert decision.state is RiskState.LOCK_ACCOUNT


class TestConnectivity:
    def test_broker_disconnected_stops_new_orders(self) -> None:
        decision = _guard().evaluate(_context(broker_connected=False))
        assert decision.state is RiskState.STOP_NEW_ORDERS

    def test_unknown_connectivity_locks(self) -> None:
        decision = _guard().evaluate(_context(broker_connected=None))
        assert decision.state is RiskState.LOCK_ACCOUNT


class TestOrderRate:
    def test_rate_limit_stops_new_orders(self) -> None:
        stamps = [NOW - timedelta(seconds=i) for i in range(61)]
        decision = _guard(max_orders_per_hour=60).evaluate(
            _context(order_timestamps=tuple(stamps))
        )
        assert decision.state is RiskState.STOP_NEW_ORDERS
        assert "order_rate" in decision.triggered_by

    def test_rate_within_limit_is_nominal(self) -> None:
        stamps = [NOW - timedelta(seconds=i) for i in range(10)]
        assert _guard().evaluate(_context(order_timestamps=tuple(stamps))).state is (
            RiskState.NOMINAL
        )


class TestDuplicateOrder:
    def test_in_flight_duplicate_rejected(self) -> None:
        guard = _guard()
        state = guard.check_duplicate_order("key-1", {"key-1": False})
        assert state is RiskState.STOP_NEW_ORDERS

    def test_completed_duplicate_rejected_by_registry_snapshot(self) -> None:
        from execution.idempotency import IdempotencyRegistry, compute_idempotency_key

        registry = IdempotencyRegistry()
        key = compute_idempotency_key(
            {
                "strategy_id": "s",
                "hypothesis_id": "h",
                "symbol": "A",
                "side": "buy",
                "quantity": 1,
                "limit_price": 1.0,
                "order_type": "limit",
                "rebalance_date": "2026-08-24",
            }
        )
        registry.claim(key)
        guard = _guard()
        assert (
            guard.check_duplicate_order(key, registry.accepted_keys())
            is RiskState.STOP_NEW_ORDERS
        )
        registry.mark_completed(key)
        assert (
            guard.check_duplicate_order(key, registry.accepted_keys())
            is RiskState.STOP_NEW_ORDERS
        )

    def test_new_key_not_duplicate(self) -> None:
        assert _guard().check_duplicate_order("fresh", {}) is None


class TestReconciliationLock:
    def test_reconciliation_lock_locks_account(self) -> None:
        decision = _guard().evaluate(_context(reconciliation_locked=True))
        assert decision.state is RiskState.LOCK_ACCOUNT
        assert decision.human_action_required is True


class TestSeverityAndTransitions:
    def test_most_severe_state_wins(self) -> None:
        decision = _guard(max_daily_loss=0.03).evaluate(
            _context(
                equity_now=900_000.0,  # daily loss AND drawdown
                equity_peak=1_000_000.0,
                reconciliation_locked=True,
            )
        )
        assert decision.state is RiskState.LOCK_ACCOUNT
        triggered = set(decision.triggered_by)
        assert triggered >= {"daily_loss", "max_drawdown", "reconciliation_lock"}

    def test_decision_serializable(self) -> None:
        decision = _guard().evaluate(_context(reconciliation_locked=True))
        payload = decision.to_dict()
        assert payload["state"] == "LOCK_ACCOUNT"
        assert payload["human_action_required"] is True

    def test_decision_maps_to_domain_model(self) -> None:
        decision = _guard().evaluate(_context())
        model = decision.to_model()
        assert model.state == "NOMINAL"


class TestLimitsValidation:
    def test_invalid_limits_rejected(self) -> None:
        with pytest.raises(RiskCheckError):
            RiskLimits(max_daily_loss=0)
        with pytest.raises(RiskCheckError):
            RiskLimits(max_drawdown=1.5)
        with pytest.raises(RiskCheckError):
            RiskLimits(max_data_age_hours=-1)
        with pytest.raises(RiskCheckError):
            RiskLimits(max_orders_per_hour=0)
