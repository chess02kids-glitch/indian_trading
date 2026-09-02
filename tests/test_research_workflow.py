"""Tests for the v0.3 production research workflow.

Covers MLflow artifact/parameter/metric logging, immutable ledger integrity
(including failed/interrupted runs and duplicate detection), long-run replay
(restart recovery and duplicate scheduler protection), paper-trading
analytics determinism, advanced research reports, and the new CLI commands.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.engine import (
    MEMBERSHIP_FROM_PRICES,
    BacktestConfig,
    VectorBTResearchEngine,
)
from research.contracts import CostModel, Experiment, ResearchInputError
from research.experiments import ExperimentManager, build_research_artifacts
from research.gate import ResearchGate
from research.ledger import (
    LEDGER_STATUSES,
    DuplicateExperimentError,
    HypothesisLedger,
)
from research.paper_analytics import Fill, PositionMark, compute_paper_analytics
from research.replay import LongRunReplay
from research.reporting import generate_advanced_report


def _result(periods: int = 80):
    index = pd.date_range("2022-01-03", periods=periods, freq="B")
    rng = np.random.default_rng(5)
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, (periods, 2)), axis=0)),
        index=index,
        columns=["A", "B"],
    )
    weights = pd.DataFrame(0.5, index=index, columns=["A", "B"])
    return VectorBTResearchEngine(
        BacktestConfig(use_vectorbt=False, cost_model=CostModel(1, 1))
    ).run(prices, weights, strategy_name="momentum-test", universe_history=MEMBERSHIP_FROM_PRICES)


class _RunContext:
    def __init__(self, run_id: str = "run-1") -> None:
        self.info = type("Info", (), {"run_id": run_id})()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _FakeMLflow:
    """MLflow-shaped fake recording params, metrics, tags, and artifacts."""

    def __init__(self) -> None:
        self.uri = None
        self.experiment = None
        self.params = None
        self.metrics = None
        self.tags = None
        self.artifacts: list[tuple[str, str]] = []

    def set_tracking_uri(self, uri: str) -> None:
        self.uri = uri

    def set_experiment(self, name: str) -> None:
        self.experiment = name

    def start_run(self, run_name: str):
        self.run_name = run_name
        return _RunContext()

    def log_params(self, params) -> None:
        self.params = params

    def log_metrics(self, metrics) -> None:
        self.metrics = metrics

    def set_tags(self, tags) -> None:
        self.tags = tags

    def log_artifact(self, path: str, artifact_path: str | None = None) -> None:
        self.artifacts.append((str(path), artifact_path or ""))


class TestMlflowArtifacts:
    def test_artifacts_are_written_and_logged(self, tmp_path: Path) -> None:
        result = _result(periods=120)
        fake = _FakeMLflow()
        manager = ExperimentManager(
            experiment_name="test",
            tracking_dir=tmp_path / "history",
            mlflow_module=fake,
            minimum_deflated_sharpe_probability=0.0 + 1e-9,
        )
        experiment = Experiment(
            "H-1", "momentum-test", {"lookback": 20}, ["momentum_1m"], "nifty50"
        )
        artifacts = build_research_artifacts(
            result,
            artifact_dir=tmp_path / "artifacts",
            experiment_id=experiment.experiment_id,
            validation={"fold_metrics": [{"sharpe": 0.5, "observations": 10}]},
            confidence_intervals={
                "sharpe": type("CI", (), {"to_dict": lambda self: {"estimate": 1.0}})()
            },
            gate_result={
                "verdict": "PASS",
                "score": 90.0,
                "checks": [{"name": "c", "status": "pass", "message": "m"}],
            },
            research_report=None,
        )
        names = set(artifacts)
        assert {
            "equity_curve.csv",
            "drawdown_series.csv",
            "returns.csv",
            "turnover.csv",
            "portfolio_weights.csv",
            "validation.json",
            "validation_fold_metrics.csv",
            "confidence_intervals.json",
            "research_gate.json",
            "drawdown_plot.html",
            "validation_plot.html",
        } <= names
        assert all(Path(path).is_file() for path in artifacts.values())

        record = manager.log_experiment(
            experiment,
            result=result,
            validation={"deflated_sharpe": {"probability": 0.99}},
            gate_result={"verdict": "PASS", "score": 90.0, "checks": []},
            artifacts=artifacts,
            strategy_version="1.2",
            factor_versions={"momentum_1m": "1.0"},
            validation_method="walk_forward",
            random_seed=42,
            dataset_fingerprint="fingerprint-1",
            git_commit="deadbeef",
            dataset_version="synthetic-v1",
            cost_model="india:base",
            backtest_period="2022-01-03/2022-06-30",
            oos_period="2022-05-01/2022-06-30",
        )
        assert record.status == "accepted"
        assert record.run_id == "run-1"
        # Parameters required by the v0.3 contract.
        assert fake.params["strategy"] == "momentum-test"
        assert fake.params["strategy_version"] == "1.2"
        assert "momentum_1m" in fake.params["factor_versions"]
        assert fake.params["universe"] == "nifty50"
        assert fake.params["cost_model"] == "india:base"
        assert fake.params["validation_method"] == "walk_forward"
        assert fake.params["random_seed"] == "42"
        assert fake.params["git_commit"] == "deadbeef"
        assert fake.params["dataset_fingerprint"] == "fingerprint-1"
        # Metrics required by the v0.3 contract.
        for required in (
            "annualized_return",
            "sharpe",
            "sortino",
            "annualized_volatility",
            "max_drawdown",
            "turnover",
            "win_rate",
            "gate_score",
        ):
            assert required in fake.metrics, required
        assert fake.tags["gate_result"].endswith("}")
        assert {path for path, _ in fake.artifacts} >= set(artifacts.values())
        # Records persist and load back with the new fields.
        loaded = manager.list_records()
        assert len(loaded) == 1
        assert loaded[0].strategy_version == "1.2"
        assert loaded[0].dataset_fingerprint == "fingerprint-1"
        assert loaded[0].artifacts == artifacts
        assert loaded[0].gate_result["verdict"] == "PASS"

    def test_local_only_mode_works_without_mlflow(self, tmp_path: Path) -> None:
        manager = ExperimentManager(
            experiment_name="local", tracking_dir=tmp_path, mlflow_module=None
        )
        record = manager.log_experiment(
            Experiment("H-2", "momentum", {}, ["m"], "universe"),
            result=_result(),
            rejected=True,
            reason="manual",
        )
        assert record.run_id == "local"
        assert manager.list_records()[0].status == "rejected"


class TestLedgerIntegrity:
    def _experiment(self, hypothesis_id: str = "HYP-00001") -> Experiment:
        return Experiment(
            hypothesis_id,
            "momentum",
            {"lookback": 63},
            ["momentum_3m"],
            "nifty100",
            dataset_version="v1",
            cost_model="india:base",
        )

    def test_statuses_include_failed_and_interrupted(self) -> None:
        assert {"accepted", "rejected", "running", "failed", "interrupted"} <= set(
            LEDGER_STATUSES
        )

    def test_failed_and_interrupted_are_recorded(self, tmp_path: Path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        failing = ledger.record_failure(
            "momentum broke", strategy="momentum", reason="exception: NaN weights"
        )
        interrupted = ledger.record_interruption(
            "run lost power", strategy="momentum", reason="host reboot"
        )
        assert failing.status == "failed"
        assert interrupted.status == "interrupted"
        records = ledger.list_records()
        assert [row.status for row in records] == ["failed", "interrupted"]

    def test_duplicate_fingerprints_detected_and_marked(self, tmp_path: Path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        experiment = self._experiment()
        first = ledger.for_experiment(
            experiment,
            status="accepted",
            dataset_version="v1",
            dataset_fingerprint="ds",
            config_fingerprint="cfg",
            code_fingerprint="code",
            backtest_period="2020-01-01/2024-12-31",
        )
        second = ledger.for_experiment(
            Experiment(
                "HYP-00002",
                "momentum",
                {"lookback": 63},
                ["momentum_3m"],
                "nifty100",
                dataset_version="v1",
                cost_model="india:base",
            ),
            status="accepted",
            dataset_version="v1",
            dataset_fingerprint="ds",
            config_fingerprint="cfg",
            code_fingerprint="code",
            backtest_period="2020-01-01/2024-12-31",
        )
        assert first.is_duplicate is False
        assert second.is_duplicate is True
        assert second.duplicate_of == "HYP-00001"
        duplicates = ledger.find_duplicates()
        assert len(duplicates) == 1
        assert list(duplicates.values())[0] == ["HYP-00001", "HYP-00002"]

    def test_duplicate_rejection_raises(self, tmp_path: Path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.for_experiment(
            self._experiment(),
            status="accepted",
            dataset_fingerprint="ds",
            config_fingerprint="cfg",
            code_fingerprint="code",
        )
        with pytest.raises(DuplicateExperimentError):
            ledger.for_experiment(
                Experiment(
                    "HYP-00002",
                    "momentum",
                    {"lookback": 63},
                    ["momentum_3m"],
                    "nifty100",
                ),
                status="accepted",
                dataset_fingerprint="ds",
                config_fingerprint="cfg",
                code_fingerprint="code",
                reject_duplicates=True,
            )

    def test_integrity_verification(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        ledger = HypothesisLedger(path)
        ledger.record(hypothesis="x", status="rejected", strategy="s")
        assert ledger.verify_integrity() == {
            "valid": True,
            "records": 1,
            "invalid_line": None,
        }
        path.write_text(
            '{"hypothesis_id": "HYP-00001", "status": "rejected"}\nnot json\n',
            encoding="utf-8",
        )
        assert ledger.verify_integrity()["valid"] is False
        assert ledger.verify_integrity()["invalid_line"] == 2

    def test_ledger_never_rewrites(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.jsonl"
        ledger = HypothesisLedger(path)
        ledger.record(hypothesis="a", status="rejected", strategy="s")
        original = path.read_text(encoding="utf-8")
        ledger.record(hypothesis="b", status="failed", strategy="s")
        assert path.read_text(encoding="utf-8").startswith(original)


class TestLongRunReplay:
    def _replay(self, tmp_path: Path, **kwargs) -> LongRunReplay:
        defaults = {
            "start": date(2024, 1, 1),
            "end": date(2024, 1, 20),
            "frequency": "B",
            "rebalance_frequency": "M",
            "seed": 42,
        }
        defaults.update(kwargs)
        return LongRunReplay(
            tmp_path, replay_id=kwargs.pop("replay_id", "r1"), **defaults
        )

    def test_schedule_is_deterministic(self, tmp_path: Path) -> None:
        first = self._replay(tmp_path).build_schedule()
        second = self._replay(tmp_path).build_schedule()
        assert first.to_dict() == second.to_dict()
        assert first.steps[:6] == (
            date(2024, 1, 1),
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
            date(2024, 1, 8),
        )
        assert len(first.steps) == 15

    def test_restart_recovery_skips_completed_steps(self, tmp_path: Path) -> None:
        replay = self._replay(tmp_path)
        calls: list[date] = []

        def step(day: date, schedule, attempt: int) -> None:
            calls.append(day)

        first = replay.run(step)
        assert len(first.completed) == 15
        second = replay.run(step)
        assert second.completed == ()
        assert len(second.skipped_duplicates) == 15
        assert len(calls) == 15

    def test_duplicate_scheduler_lock_protects_concurrent_claims(
        self, tmp_path: Path
    ) -> None:
        replay = self._replay(tmp_path)
        # Pre-claim the first step as a live other scheduler would: the
        # lock must prevent a second execution of the same step.
        lock = tmp_path / "r1.lock" / "2024-01-01"
        lock.mkdir(parents=True)
        (lock / "claim.json").write_text(
            json.dumps({"pid": __import__("os").getpid(), "token": "x"}) + "\n",
            encoding="utf-8",
        )
        outcomes: list[date] = []

        def step(day: date, schedule, attempt: int) -> None:
            outcomes.append(day)

        outcome = replay.run(step)
        # The pre-claimed step was skipped, everything else completed.
        assert date(2024, 1, 1) not in outcome.completed
        assert len(outcome.completed) == 14
        assert len(outcomes) == 14

    def test_stale_lock_recovered_after_crash(self, tmp_path: Path) -> None:
        replay = self._replay(tmp_path)
        lock = tmp_path / "r1.lock" / "2024-01-01"
        lock.mkdir(parents=True)
        # Dead PID (999999999) means the lock owner is gone: reclaim.
        (lock / "claim.json").write_text(
            json.dumps({"pid": 999999999, "token": "x"}) + "\n",
            encoding="utf-8",
        )
        outcomes: list[date] = []

        def step(day: date, schedule, attempt: int) -> None:
            outcomes.append(day)

        outcome = replay.run(step)
        assert date(2024, 1, 1) in outcome.completed
        assert len(outcome.completed) == 15

    def test_configuration_change_refuses_to_resume(self, tmp_path: Path) -> None:
        replay = self._replay(tmp_path)
        replay.run(lambda day, schedule, attempt: None)
        changed = self._replay(tmp_path, end=date(2024, 1, 21))
        with pytest.raises(ResearchInputError, match="configuration changed"):
            changed.run(lambda day, schedule, attempt: None)


class TestPaperAnalytics:
    def _fills_and_marks(self):
        ts = pd.Timestamp("2024-01-02")
        fills = [
            Fill(ts, "RELIANCE", "BUY", 10, 100.0, reference_price=100.0, order_id="a"),
            Fill(ts, "RELIANCE", "BUY", 10, 101.0, reference_price=100.0, order_id="b"),
            Fill(
                ts, "RELIANCE", "SELL", 15, 105.0, reference_price=105.0, order_id="c"
            ),
            Fill(ts, "TCS", "BUY", 5, 500.0, reference_price=500.0, order_id="d"),
        ]
        marks = [
            PositionMark(ts, "RELIANCE", 5, 100.5),
            PositionMark(ts, "TCS", 5, 500.0),
        ]
        prices = {"RELIANCE": 103.0, "TCS": 510.0}
        return fills, marks, prices

    def test_fifo_realized_and_unrealized_pnl(self) -> None:
        fills, marks, prices = self._fills_and_marks()
        analytics = compute_paper_analytics(
            fills, marks, prices, initial_cash=1_000_000.0
        )
        # 10 sold @105 matched against 10 @100 (+50) and 5 @101 (+20).
        assert analytics.realized_pnl["RELIANCE"] == pytest.approx(70.0)
        # 5 remaining @100.5 marked at 103 (+12.5); TCS 5 @500 -> 510 (+50).
        assert analytics.unrealized_pnl["RELIANCE"] == pytest.approx(12.5)
        assert analytics.unrealized_pnl["TCS"] == pytest.approx(50.0)
        assert analytics.total_realized_pnl == pytest.approx(70.0)
        assert analytics.total_unrealized_pnl == pytest.approx(62.5)
        # Slippage: second buy slipped 100 bps on 10 shares.
        assert analytics.total_slippage == pytest.approx(10.0)
        assert analytics.average_slippage_bps == pytest.approx(25.0)
        assert analytics.total_turnover == pytest.approx(6085.0)

    def test_deterministic_and_benchmark_divergence(self) -> None:
        fills, marks, prices = self._fills_and_marks()
        ts = pd.Timestamp("2024-01-02")
        benchmark = pd.Series([0.02], index=[ts])
        first = compute_paper_analytics(
            fills, marks, prices, initial_cash=1_000_000.0, benchmark_returns=benchmark
        )
        second = compute_paper_analytics(
            fills, marks, prices, initial_cash=1_000_000.0, benchmark_returns=benchmark
        )
        assert first.to_dict() == second.to_dict()
        assert first.benchmark_divergence == pytest.approx(
            (analytics_equity_return(first) - 0.02), abs=1e-9
        )

    def test_invalid_inputs_rejected(self) -> None:
        ts = pd.Timestamp("2024-01-02")
        with pytest.raises(ResearchInputError):
            Fill(ts, "X", "HOLD", 1, 1.0)
        with pytest.raises(ResearchInputError):
            compute_paper_analytics([], [], initial_cash=1_000_000.0)
        with pytest.raises(ResearchInputError):
            compute_paper_analytics(
                [Fill(ts, "X", "BUY", 1, 10.0)], [], initial_cash=-1
            )


def analytics_equity_return(analytics) -> float:
    values = list(analytics.equity_curve.values())
    return values[-1] / 1_000_000.0 - 1.0


class TestAdvancedReports:
    def test_report_contains_all_v03_fields(self, tmp_path: Path) -> None:
        result = _result(periods=150)
        report = generate_advanced_report(
            result,
            frequency="M",
            validation={
                "method": "walk_forward",
                "folds": 4,
                "positive_fold_fraction": 0.75,
            },
            gate_result={"verdict": "PASS", "score": 90.0, "checks": []},
            configuration={
                "strategy": "momentum-test",
                "parameters": {"lookback": 20},
                "rebalance_frequency": "M",
            },
            confidence_intervals={
                "sharpe": {"estimate": 0.8, "lower": 0.1, "upper": 1.5}
            },
            metadata={"git_commit": "deadbeef", "dataset_fingerprint": "fp-1"},
            generated_at=pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime(),
        )
        payload = report.to_dict()
        assert payload["gate"]["verdict"] == "PASS"
        assert payload["validation"]["folds"] == 4
        assert payload["configuration"]["rebalance_frequency"] == "M"
        assert payload["confidence_intervals"]["sharpe"]["lower"] == 0.1
        assert payload["reproducibility"]["git_commit"] == "deadbeef"
        assert "Research gate" in report.to_markdown()
        assert "Reproducibility metadata" in report.to_markdown()
        json_path, markdown_path = report.write(tmp_path)
        assert json_path.is_file() and markdown_path.is_file()

    def test_invalid_frequency_rejected(self) -> None:
        result = _result()
        with pytest.raises(ResearchInputError):
            generate_advanced_report(result, frequency="Y")


class TestResearchCli:
    def _prices_path(self, tmp_path: Path) -> Path:
        index = pd.date_range("2022-01-03", periods=400, freq="B")
        rng = np.random.default_rng(6)
        prices = pd.DataFrame(
            100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, (400, 2)), axis=0)),
            index=index,
            columns=["A", "B"],
        )
        path = tmp_path / "prices.csv"
        prices.to_csv(path, index_label="date")
        return path

    def test_gate_cli_smoke(self, tmp_path: Path, capsys) -> None:
        from research.cli import cli_main

        prices = self._prices_path(tmp_path)
        output = tmp_path / "gate"
        assert (
            cli_main(
                [
                    "research",
                    "gate",
                    "--strategy",
                    "momentum",
                    "--prices",
                    str(prices),
                    "--output-dir",
                    str(output),
                    "--placebo-samples",
                    "5",
                    "--train-size",
                    "100",
                    "--test-size",
                    "50",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] in {"PASS", "FAIL", "FRAGILE"}
        assert (output / "research_gate.json").is_file()
        assert (output / "research_gate_summary.json").is_file()

    def test_diagnostics_cli_smoke(self, tmp_path: Path, capsys) -> None:
        from research.cli import cli_main

        prices = self._prices_path(tmp_path)
        output = tmp_path / "diag"
        assert (
            cli_main(
                [
                    "research",
                    "diagnostics",
                    "--strategy",
                    "momentum",
                    "--prices",
                    str(prices),
                    "--output-dir",
                    str(output),
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert Path(payload["path"]).is_file()
        diagnostics = json.loads(Path(payload["path"]).read_text())
        assert "factor_decay" in diagnostics

    def test_advanced_report_cli_smoke(self, tmp_path: Path, capsys) -> None:
        from research.cli import cli_main

        prices = self._prices_path(tmp_path)
        output = tmp_path / "advanced"
        assert (
            cli_main(
                [
                    "report",
                    "advanced",
                    "--strategy",
                    "momentum",
                    "--prices",
                    str(prices),
                    "--output-dir",
                    str(output),
                    "--periods",
                    "M",
                    "--train-size",
                    "100",
                    "--test-size",
                    "50",
                ]
            )
            == 0
        )
        paths = json.loads(capsys.readouterr().out)
        assert "M" in paths
        payload = json.loads(Path(paths["M"]).read_text())
        assert payload["frequency"] == "M"
        assert "gate" in payload

    def test_replay_plan_cli_smoke(self, capsys) -> None:
        from research.cli import cli_main

        assert (
            cli_main(
                [
                    "replay",
                    "plan",
                    "--replay-id",
                    "nightly",
                    "--start",
                    "2024-01-01",
                    "--end",
                    "2024-01-10",
                    "--frequency",
                    "B",
                ]
            )
            == 0
        )
        payload = json.loads(capsys.readouterr().out)
        assert len(payload["steps"]) == 8
        assert payload["steps"][0] == "2024-01-01"
        assert payload["replay_id"] == "nightly"


class TestGateLoggingIntegration:
    def test_gate_verdict_becomes_experiment_metric(self, tmp_path: Path) -> None:
        result = _result(periods=300)
        fake = _FakeMLflow()
        manager = ExperimentManager(
            experiment_name="test",
            tracking_dir=tmp_path / "history",
            mlflow_module=fake,
            minimum_deflated_sharpe_probability=0.0 + 1e-9,
        )
        gate = ResearchGate(random_seed=42)
        decision = gate.evaluate(
            result,
            benchmarks={},
            generated_at=pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime(),
        )
        record = manager.log_experiment(
            Experiment("H-GATE", "momentum-test", {}, ["m"], "u"),
            result=result,
            gate_result=decision,
        )
        assert "gate_score" in fake.metrics
        assert fake.tags["gate_result"].startswith('{"benchmarks"')
        assert record.gate_result["verdict"] == decision.verdict
