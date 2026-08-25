"""Phase 3 / final-suite tests for the daily orchestration pipeline."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pandas as pd

from execution.idempotency import IdempotencyRegistry
from execution.paper import PaperBroker, PaperBrokerConfig
from execution.service import ExecutionService
from observability.alerts import AlertService
from observability.health import HealthService, SystemHealth
from orchestration.pipeline import (
    DailyPipeline,
    ManualApprovalGate,
    RecordingApprovalGate,
)
from portfolio.construction import EqualWeightConstructor
from reconciliation.engine import ReconciliationEngine
from research.strategies import MomentumStrategy
from risk_kill import RiskGuard, RiskLimits
from store import (
    InMemoryOrderRepository,
    InMemoryPositionRepository,
    InMemoryReconciliationRepository,
    InMemoryResearchRepository,
    InMemoryRunRepository,
)

T0 = datetime(2026, 8, 24, 9, 15, tzinfo=UTC)


def make_frame(
    start: str = "2026-08-10",
    days: int = 12,
    symbols: tuple[str, float, float] = (
        ("RELIANCE", 100.0, 0.002),
        ("TCS", 200.0, 0.0),
    ),
) -> pd.DataFrame:
    """Deterministic long-form OHLCV: RELIANCE trends up, TCS is flat."""
    index = pd.date_range(start, periods=days, freq="B")
    rows = []
    for symbol, base, growth in symbols:
        price = base
        for day in index:
            price = price * (1.0 + growth) if growth else price
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "open": price * 0.999,
                    "high": price * 1.001,
                    "low": price * 0.998,
                    "close": price,
                    "volume": 1_000_000,
                }
            )
    frame = pd.DataFrame(rows)
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def build_pipeline(tmp_path, *, gate=None, **guard_limits):
    broker = PaperBroker(T0, PaperBrokerConfig(fill_probability=1.0, seed=3))
    order_repo = InMemoryOrderRepository()
    position_repo = InMemoryPositionRepository()
    run_repo = InMemoryRunRepository()
    research_repo = InMemoryResearchRepository()
    recon_repo = InMemoryReconciliationRepository()
    guard = RiskGuard(
        RiskLimits(max_position_exposure=1.0, max_gross_exposure=1.0, **guard_limits)
    )
    service = ExecutionService(
        broker=broker,
        order_repository=order_repo,
        position_repository=position_repo,
        risk_guard=guard,
        idempotency_registry=IdempotencyRegistry(),
    )
    pipeline = DailyPipeline(
        strategy=MomentumStrategy(lookback=2),
        constructor=EqualWeightConstructor(),
        broker=broker,
        execution_service=service,
        risk_guard=guard,
        run_repository=run_repo,
        position_repository=position_repo,
        order_repository=order_repo,
        research_repository=research_repo,
        reconciliation_repository=recon_repo,
        reconciliation_engine=ReconciliationEngine(guard),
        health_service=HealthService(tmp_path / "status.json"),
        alert_service=AlertService(environ={}),
        approval_gate=gate or ManualApprovalGate(),
        dataset_version="test-v1",
        cash_for_allocation=1_000_000.0,
    )
    return {
        "pipeline": pipeline,
        "broker": broker,
        "orders": order_repo,
        "runs": run_repo,
        "recon": recon_repo,
        "research": research_repo,
    }


class TestDailyFlow:
    def test_daily_flow_completes(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        result = parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        assert result.status == "completed"
        assert result.health == "HEALTHY"
        assert result.signals_generated is True
        assert result.approved is True
        assert len(result.execution.submitted) == 1
        order = result.execution.submitted[0]
        assert order.symbol == "RELIANCE"
        assert order.filled_quantity > 0
        position = parts["broker"].get_positions()[0]
        assert position.quantity == order.filled_quantity
        assert result.reconciliation is not None
        assert result.reconciliation.matched is True

    def test_rejected_flat_symbol_not_traded(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        traded = {p.symbol for p in parts["broker"].get_positions() if p.quantity}
        assert traded == {"RELIANCE"}

    def test_duplicate_run_rejected(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        first = parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        second = parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        assert first.status == "completed"
        assert second.status == "duplicate_run"
        # Only one execution happened.
        assert len(parts["orders"].list_intents()) == 1

    def test_concurrent_runs_cannot_duplicate(self, tmp_path) -> None:
        """Final suite (test 10) at pipeline level: exactly one winner."""
        parts = build_pipeline(tmp_path, gate=RecordingApprovalGate())
        statuses: list[str] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            result = parts["pipeline"].run_day("daily-2026-08-24", make_frame())
            statuses.append(result.status)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(statuses) == ["completed", "duplicate_run"]
        assert len(parts["orders"].list_intents()) == 1

    def test_stale_data_prevents_signal_generation(self, tmp_path) -> None:
        """Final suite (test 7): stale data halts before any signal exists."""
        parts = build_pipeline(tmp_path)
        stale_frame = make_frame(start="2020-01-06", days=10)
        result = parts["pipeline"].run_day(
            "run-stale", stale_frame, approved_by="alice"
        )
        assert result.status == "halted_data_quality"
        assert result.signals_generated is False
        assert result.health == "HALTED"
        assert parts["broker"].get_cash() == 1_000_000.0
        assert parts["orders"].list_intents() == []

    def test_invalid_ohlc_halts_before_research(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        frame = make_frame()
        bad_row = frame[frame["symbol"] == "TCS"].iloc[0].copy()
        bad_row["high"] = bad_row["low"] - 1.0
        frame = pd.concat([frame, bad_row.to_frame().T], ignore_index=True)
        result = parts["pipeline"].run_day("run-bad", frame, approved_by="alice")
        assert result.status == "halted_data_quality"
        assert result.signals_generated is False

    def test_approval_gate_is_fail_closed(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        result = parts["pipeline"].run_day("run-2", make_frame())
        assert result.status == "awaiting_approval"
        assert result.approved is False
        assert parts["broker"].get_cash() == 1_000_000.0
        assert parts["orders"].list_intents() == []

    def test_approval_can_be_granted_for_a_run(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        gate: ManualApprovalGate = parts["pipeline"].approval_gate
        # No approval yet: the first run waits.
        first = parts["pipeline"].run_day("run-2", make_frame())
        assert first.status == "awaiting_approval"
        # An operator grants approval for the next run id: it executes.
        gate.grant_approval("run-3")
        frame3 = make_frame(start="2026-08-17")
        result = parts["pipeline"].run_day("run-3", frame3)
        assert result.status == "completed"

    def test_warmup_frame_completes_without_crash(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        # Three recent rows: only the last momentum observation exists. The
        # flow must complete cleanly whether or not an order is generated.
        tiny = make_frame(start="2026-08-20", days=3)
        result = parts["pipeline"].run_day("run-tiny", tiny, approved_by="alice")
        assert result.status == "completed"
        assert result.execution is None or len(result.execution.submitted) <= 1

    def test_broker_drift_locks_account(self, tmp_path) -> None:
        """Final suite (test 9) end to end: broker state drifting from the
        persisted order ledger locks the account."""
        parts = build_pipeline(tmp_path)
        first = parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        assert first.status == "completed"
        # Simulate broker drift: one share vanished from the broker's books.
        positions = parts["broker"].get_positions()
        drifted = [p for p in positions if p.symbol == "RELIANCE"][0]
        parts["broker"]._positions[(drifted.symbol, drifted.exchange)] = (
            drifted.model_copy(update={"quantity": drifted.quantity - 1})
        )

        flat = make_frame(
            start="2026-08-20", days=3, symbols=(("RELIANCE", 100.0, 0.0),)
        )
        result = parts["pipeline"].run_day("run-2", flat, approved_by="alice")
        assert result.status == "locked_reconciliation"
        assert result.health == "LOCKED"
        assert result.risk_state == "LOCK_ACCOUNT"
        assert result.reconciliation is not None
        assert result.reconciliation.locked is True

    def test_health_document_written(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        document = parts["pipeline"].health.read_status_document()
        assert document["system_health"] == "HEALTHY"
        assert document["latest_run"] == "run-1"
        assert document["risk_state"] == "NOMINAL"
        assert document["reconciliation"]["matched"] is True

    def test_alerts_recorded(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        events = [alert.event for alert in parts["pipeline"].alerts.list_alerts()]
        assert "daily_run_completed" in events

    def test_run_recorded_in_repository(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        record = parts["runs"].get_run("run-1")
        assert record is not None
        assert record["status"] == "completed"

    def test_research_result_persisted(self, tmp_path) -> None:
        parts = build_pipeline(tmp_path)
        parts["pipeline"].run_day("run-1", make_frame(), approved_by="alice")
        latest = parts["research"].latest_result()
        assert latest is not None
        assert latest.status == "accepted"
        assert latest.dataset_version == "test-v1"


class TestHealthService:
    def test_transitions_and_fail_closed(self, tmp_path) -> None:
        service = HealthService(tmp_path / "s.json")
        assert service.state is SystemHealth.HEALTHY
        service.set_state(SystemHealth.WARNING, "data late")
        assert service.state is SystemHealth.WARNING
        # Lower-severity states cannot overwrite higher ones.
        assert service.set_state(SystemHealth.HEALTHY, "ok") is SystemHealth.WARNING
        service.set_state(SystemHealth.LOCKED, "mismatch")
        assert service.state is SystemHealth.LOCKED
        assert service.set_state(SystemHealth.HALTED, "x") is SystemHealth.LOCKED

    def test_manual_reset(self, tmp_path) -> None:
        service = HealthService(tmp_path / "s.json")
        service.set_state(SystemHealth.LOCKED, "mismatch")
        assert service.reset("alice") is SystemHealth.HEALTHY

    def test_missing_status_file_is_unknown_not_healthy(self, tmp_path) -> None:
        service = HealthService(tmp_path / "missing.json")
        document = service.read_status_document()
        assert document["state"] == "unknown"

    def test_status_file_round_trip(self, tmp_path) -> None:
        path = tmp_path / "s.json"
        service = HealthService(path)
        service.set_state(SystemHealth.HALTED, "stale")
        assert path.exists()
        reloaded = HealthService(path).read_status_document()
        assert reloaded["state"] == "HALTED"


class TestAlertService:
    def test_severities_recorded(self) -> None:
        service = AlertService(environ={})
        service.info("started")
        service.warning("late data")
        service.critical("kill switch")
        alerts = service.list_alerts()
        assert [a.severity.value for a in alerts] == ["INFO", "WARNING", "CRITICAL"]
        # No credentials -> no delivery attempts.
        assert service.deliveries == []

    def test_credentials_only_from_environment(self, monkeypatch) -> None:
        service = AlertService(environ={})
        assert service.telegram_configured() is False
        service2 = AlertService(
            environ={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        )
        assert service2.telegram_configured() is True

    def test_delivery_failure_is_not_fatal(self) -> None:
        service = AlertService(
            environ={"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "y"},
            timeout_seconds=0.2,
        )
        alert = service.critical("boom")  # must not raise
        assert alert.severity.value == "CRITICAL"
        assert len(service.deliveries) == 1
        assert service.deliveries[0]["delivered"] is False


class TestDashboardPaper:
    def test_summarize_status(self, tmp_path) -> None:
        from dashboard.paper_dashboard import summarize_status

        document = {
            "system_health": "HEALTHY",
            "risk_state": "NOMINAL",
            "latest_run": "run-9",
            "reconciliation": {"matched": True, "locked": False},
            "paper_positions": [{"symbol": "A", "quantity": 1}],
            "open_orders": [],
            "alerts_recent": [{"severity": "INFO", "event": "ok"}],
        }
        view = summarize_status(document)
        assert view["health"] == "HEALTHY"
        assert view["reconciliation"] == "matched"
        assert view["positions"][0]["symbol"] == "A"

    def test_summarize_missing_is_unknown(self, tmp_path) -> None:
        from dashboard.paper_dashboard import summarize_status

        view = summarize_status(None)
        assert view["health"] == "unknown"
        assert view["risk_state"] == "unknown"

    def test_load_json_missing_and_malformed(self, tmp_path) -> None:
        from dashboard.paper_dashboard import load_json

        assert load_json(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert load_json(bad) is None

    def test_status_path_environment(self, monkeypatch) -> None:
        from dashboard.paper_dashboard import status_file_path
        from pathlib import Path

        monkeypatch.setenv("QUANT_INDIA_PAPER_STATUS", "/tmp/custom.json")
        assert status_file_path() == Path("/tmp/custom.json")
