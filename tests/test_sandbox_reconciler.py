"""Sandbox reconciliation: polling, local-state sync, mismatch detection, EOD."""

from __future__ import annotations

from datetime import UTC, datetime

from broker.reconciler import SandboxReconciler, record_to_result
from broker.simulated import PartialFillFault, PendingFault
from models.domain import OrderResult, OrderStatus, Position
from risk_kill import RiskState
from store.memory import (
    InMemoryOrderRepository,
    InMemoryPositionRepository,
    InMemoryReconciliationRepository,
)
from tests.sandbox_common import T0, SandboxEnv, SleepLog, make_intent


def _bootstrap(tmp_path, name="sb"):
    env = SandboxEnv(tmp_path / name, "upstox")
    env.login()
    orders = InMemoryOrderRepository()
    positions = InMemoryPositionRepository()
    recon_repo = InMemoryReconciliationRepository()
    reconciler = SandboxReconciler(
        env.adapter,
        order_repository=orders,
        position_repository=positions,
        reconciliation_repository=recon_repo,
        sleep=lambda s: None,
    )
    return env, orders, positions, recon_repo, reconciler


class TestPolling:
    def test_terminal_immediately(self, tmp_path) -> None:
        env, _, _, _, reconciler = _bootstrap(tmp_path)
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        polled = reconciler.poll_order(record.order_id)
        assert polled is not None and polled.status is OrderStatus.FILLED

    def test_pending_advances_to_fill(self, tmp_path) -> None:
        env, _, _, _, reconciler = _bootstrap(tmp_path)
        env.transport.script("place", [PendingFault(polls=2)])
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        assert record.status is OrderStatus.PENDING
        latest = reconciler.poll_order(record.order_id)
        assert latest is not None and latest.status is OrderStatus.FILLED
        assert latest.filled_quantity == 10

    def test_poll_budget_exhausted_returns_latest(self, tmp_path) -> None:
        env, _, _, _, reconciler = _bootstrap(tmp_path)
        env.transport.script("place", [PendingFault(polls=99)])
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        latest = reconciler.poll_order(record.order_id)
        assert latest is not None and latest.status is OrderStatus.PENDING

    def test_unknown_order_returns_none(self, tmp_path) -> None:
        _, _, _, _, reconciler = _bootstrap(tmp_path)
        assert reconciler.poll_order("ghost") is None

    def test_poll_interval_used_decay(self, tmp_path) -> None:
        sleeps = SleepLog()
        env = SandboxEnv(tmp_path / "pi", "upstox")
        env.login()
        env.transport.script("place", [PendingFault(polls=3)])
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        reconciler = SandboxReconciler(
            env.adapter, max_polls=5, poll_interval_seconds=0.1, sleep=sleeps
        )
        latest = reconciler.poll_order(record.order_id)
        assert latest is not None and latest.status is OrderStatus.FILLED
        assert sleeps.calls == [0.1, 0.1]


class TestLocalStateSync:
    def test_sync_order_persists_result_and_position(self, tmp_path) -> None:
        env, orders, positions, _, reconciler = _bootstrap(tmp_path)
        record = env.adapter.place_limit_order(
            make_intent("ord-1", quantity=6, price=100.0)
        )
        result = reconciler.sync_order(record.order_id)
        assert result is not None and result.status is OrderStatus.FILLED
        stored = orders.get_result(result.internal_order_id)
        assert stored is not None and stored.filled_quantity == 6
        position = positions.get_position("RELIANCE")
        assert position is not None and position.quantity == 6

    def test_sync_unknown_returns_none(self, tmp_path) -> None:
        _, _, _, _, reconciler = _bootstrap(tmp_path)
        assert reconciler.sync_order("ghost") is None


class TestMismatchDetection:
    def test_matched_when_broker_and_local_agree(self, tmp_path) -> None:
        env, orders, positions, _, reconciler = _bootstrap(tmp_path)
        intent = make_intent("ord-1", quantity=10, price=100.0)
        orders.save_intent(intent)
        record = env.adapter.place_limit_order(intent)
        orders.save_result(record_to_result(record))
        positions.upsert_position(
            Position(symbol="RELIANCE", quantity=10, average_price=100.0)
        )
        result = reconciler.reconcile_now(
            "run-1", as_of=datetime(2026, 8, 25, 15, 30, tzinfo=UTC)
        )
        assert result.matched
        assert not result.locked

    def test_unexpected_broker_position_locks(self, tmp_path) -> None:
        env, _, _, recon_repo, reconciler = _bootstrap(tmp_path)
        # broker-side order the local system never recorded
        env.adapter.place_limit_order(make_intent("ghost-1", quantity=5, price=100.0))
        result = reconciler.end_of_day("eod-1")
        assert not result.matched
        assert result.locked
        kinds = {m.kind for m in result.mismatches}
        assert "unexpected_position" in kinds
        persisted = recon_repo.latest_result()
        assert persisted is not None and persisted.locked

    def test_missing_fill_detected(self, tmp_path) -> None:
        env, orders, _, _, reconciler = _bootstrap(tmp_path)
        intent = make_intent("ord-1", quantity=10, price=100.0)
        orders.save_intent(intent)
        orders.save_result(
            OrderResult.model_validate(
                {
                    "internal_order_id": intent.internal_order_id,
                    "idempotency_key": intent.idempotency_key,
                    "symbol": "RELIANCE",
                    "status": OrderStatus.FILLED,
                    "requested_quantity": 10,
                    "filled_quantity": 10,
                    "average_fill_price": 100.0,
                    "timestamp": T0,
                }
            )
        )
        # broker has no such order -> missing_fill mismatch
        result = reconciler.reconcile_now("run-2")
        assert not result.matched
        assert any(m.kind == "missing_fill" for m in result.mismatches)

    def test_partial_fill_quantity_mismatch(self, tmp_path) -> None:
        env, orders, positions, _, reconciler = _bootstrap(tmp_path)
        env.transport.script("place", [PartialFillFault(0.5)])
        intent = make_intent("ord-1", quantity=10, price=100.0)
        orders.save_intent(intent)
        record = env.adapter.place_limit_order(intent)
        broker_result = record_to_result(record)
        # local expectation claims a *full* fill
        orders.save_result(
            OrderResult.model_validate(
                {
                    "internal_order_id": intent.internal_order_id,
                    "idempotency_key": intent.idempotency_key,
                    "symbol": "RELIANCE",
                    "status": OrderStatus.FILLED,
                    "requested_quantity": 10,
                    "filled_quantity": 10,
                    "average_fill_price": 100.0,
                    "timestamp": T0,
                }
            )
        )
        positions.upsert_position(
            Position(symbol="RELIANCE", quantity=5, average_price=100.0)
        )
        result = reconciler.reconcile_now("run-3")
        assert not result.matched
        assert any(m.kind == "partial_fill" for m in result.mismatches)
        assert broker_result.filled_quantity == 5

    def test_duplicate_orders_detected_via_engine(self, tmp_path) -> None:
        env, _, _, _, reconciler = _bootstrap(tmp_path)
        order_a = OrderResult.model_validate(
            {
                "internal_order_id": "a",
                "idempotency_key": "same-key",
                "symbol": "RELIANCE",
                "status": OrderStatus.PENDING,
                "requested_quantity": 1,
                "timestamp": T0,
            }
        )
        order_b = order_a.model_copy(update={"internal_order_id": "b"})
        result = reconciler.reconcile_now(
            "run-4",
            expected={"expected_filled": {}},
            actual={"actual_orders": [order_a, order_b]},
        )
        assert not result.matched
        assert any(m.kind == "duplicate_order" for m in result.mismatches)


class TestEndOfDayAndRisk:
    def test_locked_result_maps_to_lock_account(self, tmp_path) -> None:
        env, _, _, _, reconciler = _bootstrap(tmp_path)
        env.adapter.place_limit_order(make_intent("ghost", quantity=5))
        result = reconciler.end_of_day("eod-2")
        decision = reconciler.risk_decision(result)
        assert decision.state is RiskState.LOCK_ACCOUNT

    def test_matched_result_maps_to_nominal(self, tmp_path) -> None:
        _, _, _, _, reconciler = _bootstrap(tmp_path)
        result = reconciler.end_of_day("eod-3")
        assert result.matched
        decision = reconciler.risk_decision(result)
        assert decision.state is RiskState.NOMINAL

    def test_end_of_day_persists_result(self, tmp_path) -> None:
        _, _, _, recon_repo, reconciler = _bootstrap(tmp_path)
        reconciler.end_of_day("eod-4")
        latest = recon_repo.latest_result("eod-4")
        assert latest is not None and latest.run_id == "eod-4"
