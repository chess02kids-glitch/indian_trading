"""Safe execution layer: LIMIT-only enforcement, duplicates, gating, service wiring."""

from __future__ import annotations

from datetime import timedelta

import pytest

from broker.errors import LiveTradingDisabledError
from broker.mode import OperatingMode
from broker.rate_limit import RateLimiter
from broker.safe_execution import SandboxExecutionAdapter
from broker.simulated import PendingFault, TimeoutFault
from execution.adapter import ExecutionAdapter
from execution.idempotency import compute_idempotency_key
from execution.service import ExecutionService
from execution.validation import OrderValidationError
from models.domain import OrderStatus, PortfolioTarget
from risk_kill import RiskContext, RiskGuard
from store.memory import InMemoryOrderRepository, InMemoryPositionRepository
from tests.sandbox_common import (
    T0,
    FakeClock,
    FakeMono,
    SandboxEnv,
    SleepLog,
    make_intent,
)


def _executor(env: SandboxEnv, **overrides) -> SandboxExecutionAdapter:
    kwargs: dict = {"sleep": env.sleeps}
    kwargs.update(overrides)
    return SandboxExecutionAdapter(env.adapter, **kwargs)


class TestProtocolConformance:
    def test_satisfies_execution_adapter_protocol(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        assert isinstance(_executor(env), ExecutionAdapter)

    def test_mode_is_sandbox(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        assert _executor(env).mode is OperatingMode.SANDBOX

    def test_constructor_refuses_live_adapter(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.adapter._mode = OperatingMode.LIVE
        with pytest.raises(LiveTradingDisabledError):
            _executor(env)


class TestLimitOnlyEnforcement:
    @pytest.mark.parametrize("bad_type", ["MARKET", "IOC", "market", "ioc"])
    def test_non_limit_orders_rejected_before_broker(self, tmp_path, bad_type) -> None:
        """MARKET/IOC must never reach broker submission."""
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        executor = _executor(env)
        intent = make_intent("ord-1")
        bad = intent.model_dump(mode="python") | {"order_type": bad_type}
        with pytest.raises(OrderValidationError):
            executor.submit_order(bad, 100.0)  # type: ignore[arg-type]
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("upstox")
        )
        assert orders == []

    def test_far_limit_price_rejected_by_band(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        executor = _executor(env)
        with pytest.raises(OrderValidationError, match="band"):
            executor.submit_order(make_intent(price=100.0), 200.0)


class TestDuplicateProtection:
    def test_same_intent_returns_same_result_no_second_order(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        executor = _executor(env)
        intent = make_intent("ord-1")
        first = executor.submit_order(intent, 100.0)
        second = executor.submit_order(intent, 100.0)
        assert first is second
        assert second.broker_order_id is not None
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("upstox")
        )
        assert len(orders) == 1

    def test_same_key_different_id_deduplicated(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        env.login()
        executor = _executor(env)
        first = executor.submit_order(make_intent("ord-1"), 100.0)
        key = compute_idempotency_key(
            {
                "strategy_id": "s",
                "hypothesis_id": "h",
                "symbol": "RELIANCE",
                "side": "BUY",
                "quantity": 10,
                "limit_price": 100.0,
                "order_type": "limit",
                "rebalance_date": "2026-08-25",
            }
        )
        second = executor.submit_order(make_intent("ord-2", key=key), 100.0)
        assert second.internal_order_id == first.internal_order_id
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("dhan")
        )
        assert len(orders) == 1


class TestTokenGate:
    def test_missing_token_rejects_without_broker_call(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        executor = _executor(env)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.REJECTED
        assert "authentication expired" in (result.reason or "")
        orders = env.backend().list_orders(token=env.login())
        assert orders == []

    def test_expired_token_rejects(self, tmp_path) -> None:
        clock = FakeClock()
        env = SandboxEnv(tmp_path, "upstox", clock=clock, token_ttl_hours=1.0)
        env.login()
        clock.advance(timedelta(hours=2))
        executor = _executor(env)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.REJECTED
        assert "authentication expired" in (result.reason or "")


class TestSubmissionOutcomes:
    def test_filled_maps_broker_fields(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        executor = _executor(env)
        result = executor.submit_order(
            make_intent("ord-1", quantity=4, price=100.0), 100.0
        )
        assert result.status is OrderStatus.FILLED
        assert result.filled_quantity == 4
        assert result.broker_order_id == "upstox-sbx-00000001"
        assert result.average_fill_price == pytest.approx(100.0)

    def test_timeout_exhaustion_yields_unknown_and_blocks_retry(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        env.login()
        env.transport.script("place", [TimeoutFault(), TimeoutFault(), TimeoutFault()])
        executor = _executor(env, max_attempts=2, base_delay=0.1)
        intent = make_intent("ord-1")
        result = executor.submit_order(intent, 100.0)
        assert result.status is OrderStatus.UNKNOWN
        assert "reconcile" in (result.reason or "")
        # a retry of the same logical order must NOT resubmit blindly
        again = executor.submit_order(intent, 100.0)
        assert again is result
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("dhan")
        )
        assert orders == []

    def test_timeout_then_success_via_retry(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        env.transport.script("place", [TimeoutFault()])
        sleeps = SleepLog()
        executor = _executor(env, sleep=sleeps, max_attempts=3, base_delay=0.5)
        result = executor.submit_order(make_intent("ord-1"), 100.0)
        assert result.status is OrderStatus.FILLED
        assert sleeps.calls == [0.5]

    def test_rate_limiter_paces_submissions(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        mono = FakeMono()
        limiter = RateLimiter(1.0, clock=mono, sleep=lambda s: mono.advance(s))
        executor = SandboxExecutionAdapter(
            env.adapter, rate_limiter=limiter, sleep=env.sleeps
        )
        executor.submit_order(make_intent("ord-1"), 100.0)
        first_slot = mono.value
        executor.submit_order(
            make_intent("ord-2", symbol="TCS", rebalance_date="2026-08-26"), 100.0
        )
        assert mono.value - first_slot >= 1.0 - 1e-9

    def test_open_orders_and_cancel(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        executor = _executor(env)
        env.transport.script("place", [PendingFault(polls=9)])
        intent = make_intent("ord-1")
        pending = executor.submit_order(intent, 100.0)
        assert pending.status is OrderStatus.PENDING
        open_orders = executor.get_open_orders()
        assert [o.internal_order_id for o in open_orders] == ["ord-1"]
        cancelled = executor.cancel_order(pending.broker_order_id or "ord-1")
        assert cancelled is not None and cancelled.status is OrderStatus.CANCELLED
        assert executor.get_open_orders() == []

    def test_status_tracking_after_pending_fill(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        env.login()
        env.transport.script("place", [PendingFault(polls=2)])
        executor = _executor(env)
        intent = make_intent("ord-1")
        assert executor.submit_order(intent, 100.0).status is OrderStatus.PENDING
        latest = executor.get_order_status("ord-1")
        assert latest is not None and latest.status is OrderStatus.PENDING
        latest = executor.get_order_status("ord-1")
        assert latest is not None and latest.status is OrderStatus.FILLED

    def test_get_positions(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        executor = _executor(env)
        executor.submit_order(make_intent("ord-1", quantity=2, price=100.0), 100.0)
        positions = executor.get_positions()
        assert [(p.symbol, p.quantity) for p in positions] == [("RELIANCE", 2)]


class TestExecutionServiceIntegration:
    def _service(self, env: SandboxEnv) -> ExecutionService:
        executor = _executor(env)
        return ExecutionService(
            broker=executor,
            order_repository=InMemoryOrderRepository(),
            position_repository=InMemoryPositionRepository(),
            risk_guard=RiskGuard(),
        )

    def _context(self, **overrides) -> RiskContext:
        base = dict(
            now=T0,
            equity_now=1e6,
            equity_day_start=1e6,
            equity_peak=1e6,
            position_exposure={},
            gross_exposure=0.0,
            data_last_updated=T0,
            broker_connected=True,
            order_timestamps=(),
            reconciliation_locked=False,
        )
        base.update(overrides)
        return RiskContext(**base)

    def test_full_pipeline_research_to_sandbox(self, tmp_path) -> None:
        """Portfolio target -> intents -> risk guard -> sandbox broker fill."""
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        service = self._service(env)
        target = PortfolioTarget(
            strategy_id="S1",
            hypothesis_id="H1",
            as_of=T0.date(),
            limits={"RELIANCE": 100.0},
            target_quantities={"RELIANCE": 10},
        )
        summary = service.execute_targets(
            target,
            run_id="run-1",
            reference_prices={"RELIANCE": 100.0},
            risk_context=self._context(),
            now=T0,
        )
        assert not summary.halted
        assert [r.status for r in summary.submitted] == [OrderStatus.FILLED]
        positions = service.position_repository.list_positions()
        assert [(p.symbol, p.quantity) for p in positions] == [("RELIANCE", 10)]

    def test_risk_protective_state_halts_before_broker(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "dhan")
        env.login()
        service = self._service(env)
        target = PortfolioTarget(
            strategy_id="S1",
            hypothesis_id="H1",
            as_of=T0.date(),
            limits={"RELIANCE": 100.0},
            target_quantities={"RELIANCE": 10},
        )
        summary = service.execute_targets(
            target,
            run_id="run-2",
            reference_prices={"RELIANCE": 100.0},
            risk_context=self._context(reconciliation_locked=True),
            now=T0,
        )
        assert summary.halted
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("dhan")
        )
        assert orders == []

    def test_reexecution_is_idempotent(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path, "upstox")
        env.login()
        service = self._service(env)
        target = PortfolioTarget(
            strategy_id="S1",
            hypothesis_id="H1",
            as_of=T0.date(),
            limits={"RELIANCE": 100.0},
            target_quantities={"RELIANCE": 10},
        )
        first = service.execute_targets(
            target,
            run_id="run-1",
            reference_prices={"RELIANCE": 100.0},
            risk_context=self._context(),
            now=T0,
        )
        assert [r.status for r in first.submitted] == [OrderStatus.FILLED]
        # second identical pass: positions already match the target, so no
        # new orders are produced at all
        second = service.execute_targets(
            target,
            run_id="run-1",
            reference_prices={"RELIANCE": 100.0},
            risk_context=self._context(),
            now=T0,
        )
        assert second.submitted == []
        orders = env.backend().list_orders(
            token=env.adapter.token_manager.get_token("upstox")
        )
        assert len(orders) == 1
