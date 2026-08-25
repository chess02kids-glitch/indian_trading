"""Tests for MLflow experiment tracking, report generation, and CLI workflows."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, VectorBTResearchEngine
from research.cli import build_parser, cli_main
from research.contracts import CostModel, Experiment
from research.experiments import ExperimentManager
from research.reporting import generate_report


def _result(periods: int = 80):
    """Create a deterministic result without requiring a provider or notebook."""
    index = pd.date_range("2022-01-03", periods=periods, freq="B")
    prices = pd.DataFrame(
        {
            "A": 100 * 1.001 ** np.arange(periods),
            "B": 100 * 1.0005 ** np.arange(periods),
        },
        index=index,
    )
    weights = pd.DataFrame(0.5, index=index, columns=prices.columns)
    return VectorBTResearchEngine(
        BacktestConfig(use_vectorbt=False, cost_model=CostModel(1, 1))
    ).run(prices, weights, strategy_name="momentum", universe_history=[])


class _RunContext:
    """Minimal MLflow run context used to verify tracking calls."""

    def __init__(self) -> None:
        self.info = type("Info", (), {"run_id": "run-1"})()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None


class _FakeMLflow:
    """MLflow-shaped fake that records parameters, metrics, and tags."""

    def __init__(self) -> None:
        self.uri = None
        self.experiment = None
        self.params = None
        self.metrics = None
        self.tags = None

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


def test_experiment_manager_logs_accepted_and_rejected_records(tmp_path: Path) -> None:
    """MLflow and JSONL history capture complete experiment metadata."""
    fake = _FakeMLflow()
    manager = ExperimentManager(
        experiment_name="test",
        tracking_dir=tmp_path / "history",
        mlflow_module=fake,
        minimum_deflated_sharpe_probability=0.0 + 1e-9,
    )
    experiment = Experiment(
        "H-1", "momentum", {"lookback": 20}, ["momentum_1m"], "nifty50"
    )
    record = manager.log_experiment(
        experiment,
        result=_result(),
        validation={"walk_forward": "passed"},
        benchmarks={"buy_and_hold": _result()},
        rejected=True,
        reason="manual review",
    )
    assert record.status == "rejected"
    assert record.run_id == "run-1"
    assert fake.params["hypothesis_id"] == "H-1"
    assert "deflated_sharpe_probability" in fake.metrics
    assert fake.tags["status"] == "rejected"
    loaded = manager.list_records()
    assert len(loaded) == 1
    assert loaded[0].reason == "manual review"


def test_research_report_is_machine_and_human_readable(tmp_path: Path) -> None:
    """Reports contain required series and write both supported formats."""
    result = _result()
    report = generate_report(result, rolling_window=10, validation={"passed": True})
    payload = json.loads(report.to_json())
    assert payload["strategy"] == "momentum"
    assert payload["cumulative_returns"]
    assert payload["drawdowns"]
    assert payload["rolling_sharpes"][0]["value"] is None
    assert payload["turnover"]
    assert payload["metadata"]["backend"] == "pandas"
    assert "Reproducibility metadata" in report.to_markdown()
    assert "Benchmark comparison" in report.to_markdown()
    json_path, markdown_path = report.write(tmp_path / "reports")
    assert json_path.is_file()
    assert markdown_path.is_file()


def test_cli_parser_and_research_command_smoke(tmp_path: Path, capsys) -> None:
    """CLI parsing and report generation work with a synthetic wide price file."""
    index = pd.date_range("2022-01-03", periods=100, freq="B")
    prices = pd.DataFrame(
        {"A": 100 * 1.001 ** np.arange(100), "B": 100 * 1.0005 ** np.arange(100)},
        index=index,
    )
    path = tmp_path / "prices.csv"
    prices.to_csv(path, index_label="date")
    parser = build_parser()
    parsed = parser.parse_args(["report", "generate", "--prices", str(path)])
    assert parsed.domain == "report"
    output_dir = tmp_path / "output"
    assert (
        cli_main(
            [
                "report",
                "generate",
                "--strategy",
                "momentum",
                "--prices",
                str(path),
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )
    assert (output_dir / "momentum.json").is_file()
    assert (output_dir / "momentum.md").is_file()
    capsys.readouterr()

    assert (
        cli_main(
            [
                "research",
                "compare",
                "--strategy",
                "momentum",
                "--prices",
                str(path),
                "--output-dir",
                str(output_dir / "compare"),
            ]
        )
        == 0
    )
    assert "momentum" in json.loads(capsys.readouterr().out)
    assert (
        cli_main(
            [
                "research",
                "validate",
                "--strategy",
                "momentum",
                "--prices",
                str(path),
                "--train-size",
                "20",
                "--test-size",
                "10",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["windows"]


def test_cli_experiment_list_smoke(tmp_path: Path, capsys) -> None:
    """Experiment listing returns valid JSON even with no local records."""
    assert cli_main(["experiments", "list", "--tracking-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_report_periods_smoke(tmp_path: Path, capsys) -> None:
    """The ``report periods`` subcommand writes daily/weekly/monthly files."""
    index = pd.date_range("2022-01-03", periods=120, freq="B")
    prices = pd.DataFrame(
        {"A": 100 * 1.001 ** np.arange(120), "B": 100 * 1.0005 ** np.arange(120)},
        index=index,
    )
    path = tmp_path / "prices.csv"
    prices.to_csv(path, index_label="date")
    output_dir = tmp_path / "periods"
    assert (
        cli_main(
            [
                "report",
                "periods",
                "--strategy",
                "momentum",
                "--prices",
                str(path),
                "--output-dir",
                str(output_dir),
                "--periods",
                "W",
                "M",
            ]
        )
        == 0
    )
    paths = json.loads(capsys.readouterr().out)
    assert "W" in paths and "M" in paths
    assert Path(paths["M"]).is_file()
    payload = json.loads(Path(paths["M"]).read_text())
    assert payload["period"] == "M"
