"""Failure injection: deterministic handling of sandbox failure modes.

Covers the mission's failure list — timeout, rejection, partial fill,
duplicate request, stale token, disconnect/reconnect — and proves each is
handled deterministically and replayably.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from broker.reconciler import SandboxReconciler
from broker.simulated import (
    DisconnectFault,
    PartialFillFault,
    PendingFault,
    RejectFault,
    StaleTokenFault,
    TimeoutFault,
)
from models.domain import OrderStatus
from tests.sandbox_common import FakeClock, SandboxEnv, SleepLog, make_intent


def _env(tmp_path, name="sb"):
    env = SandboxEnv(tmp_path / name, "upstox")
    env.login()
    return env


class TestTimeoutRecovery:
    def test_single_timeout_recovers_on_retry(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        env = _env(tmp_path)
        env.transport.script("place", [TimeoutFault()])
        sleeps = SleepLog()
        executor = SandboxExecutionAdapter(env.adapter, sleep=sleeps, base_delay=0.25)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.FILLED
        assert sleeps.calls == [0.25]

    def test_consecutive_timeouts_then_recovery(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        env = _env(tmp_path)
        env.transport.script("place", [TimeoutFault(), TimeoutFault()])
        sleeps = SleepLog()
        executor = SandboxExecutionAdapter(env.adapter, sleep=sleeps, base_delay=0.25)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.FILLED
        assert sleeps.calls == [0.25, 0.5]  # exponential backoff

    def test_status_poll_survives_transient_timeout(self, tmp_path) -> None:
        env = _env(tmp_path)
        env.transport.script("place", [PendingFault(polls=1)])
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        assert record.status is OrderStatus.PENDING
        env.transport.script("status", [TimeoutFault()])
        reconciler = SandboxReconciler(env.adapter, sleep=lambda s: None, max_polls=3)
        latest = reconciler.poll_order(record.order_id)
        assert latest is not None and latest.status is OrderStatus.FILLED


class TestRejection:
    def test_injected_rejection_maps_to_rejected(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        env = _env(tmp_path)
        env.transport.script("place", [RejectFault("sandbox risk checks failed")])
        executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.REJECTED
        assert result.reason == "sandbox risk checks failed"
        # the broker retains the rejected order record for forensics
        broker_view = env.adapter.get_order_status(
            executor.submitted_results()[result.idempotency_key].broker_order_id
            or result.idempotency_key
        )
        assert broker_view is not None and broker_view.status is OrderStatus.REJECTED

    def test_rejection_fault_is_consumed_once(self, tmp_path) -> None:
        env = _env(tmp_path)
        env.transport.script("place", [RejectFault("once")])
        first = env.adapter.place_limit_order(make_intent("ord-1"))
        second = env.adapter.place_limit_order(
            make_intent("ord-2", symbol="TCS", rebalance_date="2026-08-26")
        )
        assert first.status is OrderStatus.REJECTED
        assert second.status is OrderStatus.FILLED


class TestPartialFill:
    def test_partial_fill_deterministic_fraction(self, tmp_path) -> None:
        env = _env(tmp_path)
        env.transport.script("place", [PartialFillFault(0.4)])
        record = env.adapter.place_limit_order(make_intent("ord-1", quantity=10))
        assert record.status is OrderStatus.PARTIALLY_FILLED
        assert record.filled_quantity == 4
        # funds/positions reflect only the filled portion
        position = env.adapter.get_positions()[0]
        assert position.quantity == 4
        assert env.adapter.get_funds().available_cash == pytest.approx(
            1_000_000.0 - 400.0
        )

    def test_fraction_guard(self) -> None:
        with pytest.raises(Exception):
            PartialFillFault(1.0)
        with pytest.raises(Exception):
            PartialFillFault(0.0)


class TestDuplicateRequest:
    def test_broker_side_duplicate_returns_original(self, tmp_path) -> None:
        env = _env(tmp_path)
        first = env.adapter.place_limit_order(make_intent("ord-1"))
        second = env.adapter.place_limit_order(make_intent("ord-1"))
        assert second.duplicate
        assert second.order_id == first.order_id
        assert (
            len(
                env.backend().list_orders(
                    token=env.adapter.token_manager.get_token("upstox")
                )
            )
            == 1
        )

    def test_client_side_duplicate_never_reaches_broker(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        env = _env(tmp_path)
        executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
        intent = make_intent("ord-1")
        executor.submit_order(intent, 100.0)
        duplicate = executor.submit_order(intent, 100.0)
        assert duplicate.filled_quantity == 10  # original result, unchanged
        assert len(env.adapter.get_trade_history()) == 1


class TestStaleToken:
    def test_broker_side_stale_token_maps_to_rejected(self, tmp_path) -> None:
        """Server-side invalidation (client token still valid) -> REJECTED."""
        from broker.safe_execution import SandboxExecutionAdapter

        env = _env(tmp_path)
        env.transport.script("place", [StaleTokenFault()])
        executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.REJECTED
        assert "authentication" in (result.reason or "")
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("upstox")
        )
        assert orders == []

    def test_client_side_expiry_detection_precedes_broker(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        clock = FakeClock()
        env = SandboxEnv(tmp_path / "cd", "upstox", clock=clock, token_ttl_hours=8.0)
        env.login()
        clock.advance(timedelta(hours=9))
        executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.REJECTED
        assert "expired" in (result.reason or "")


class TestReconnect:
    def test_disconnect_then_reconnect_succeeds(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        env = _env(tmp_path)
        env.transport.script("place", [DisconnectFault()])
        executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.FILLED

    def test_disconnect_never_partially_applies(self, tmp_path) -> None:
        env = _env(tmp_path)
        env.transport.script("place", [DisconnectFault()])
        with pytest.raises(Exception):
            env.adapter.place_limit_order(make_intent("ord-1"))
        # nothing reached the exchange: funds untouched, no orders
        assert env.adapter.get_funds().available_cash == pytest.approx(1_000_000.0)
        assert (
            env.backend().list_orders(
                token=env.adapter.token_manager.get_token("upstox")
            )
            == []
        )
        # reconnect: the next placement works
        recovered = env.adapter.place_limit_order(
            make_intent("ord-2", rebalance_date="2026-08-26")
        )
        assert recovered.status is OrderStatus.FILLED


class TestDeterministicReplay:
    def test_same_script_replays_identically(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter

        def run(root) -> list[str]:
            env = SandboxEnv(root, "upstox")
            env.login()
            env.transport.script(
                "place",
                [
                    TimeoutFault(),
                    RejectFault("no"),
                    PartialFillFault(0.5),
                    DisconnectFault(),
                ],
            )
            executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
            outcomes = []
            outcomes.append(
                executor.submit_order(
                    make_intent("o1", symbol="A", rebalance_date="2026-08-25"), 100.0
                ).status.value
            )
            outcomes.append(
                executor.submit_order(
                    make_intent("o2", symbol="B", rebalance_date="2026-08-26"), 100.0
                ).status.value
            )
            outcomes.append(
                executor.submit_order(
                    make_intent("o3", symbol="C", rebalance_date="2026-08-27"), 100.0
                ).status.value
            )
            return outcomes

        first = run(tmp_path / "r1")
        second = run(tmp_path / "r2")
        # Fault queue: [Timeout, Reject, Partial, Disconnect].
        #  o1: Timeout -> retry consumes next fault (Reject) -> REJECTED
        #  o2: Partial -> PARTIALLY_FILLED
        #  o3: Disconnect -> retry (queue empty) -> FILLED
        # Both runs replay identically: determinism is the property tested.
        assert first == second == ["REJECTED", "PARTIALLY_FILLED", "FILLED"]

    def test_wildcard_script_applies_to_monitored_actions(self, tmp_path) -> None:
        env = _env(tmp_path)
        env.transport.script("*", [TimeoutFault()])
        assert not env.adapter.ping()
