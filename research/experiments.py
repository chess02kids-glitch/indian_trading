"""MLflow-backed experiment tracking with an auditable local history.

Every research run becomes a first-class research record:

* **parameters** — strategy, strategy version, factor versions, universe,
  rebalance frequency, cost model, validation method, random seed, git
  commit, and dataset fingerprint;
* **metrics** — CAGR, Sharpe, Sortino, volatility, max drawdown, turnover,
  hit rate, deflated-Sharpe probability, and the gate verdict/score;
* **artifacts** — equity curve, drawdown plot and series, confidence
  intervals, validation plots, portfolio weights, factor diagnostics, and
  the research report.

MLflow is metadata-only: the research *store* remains DuckDB and Parquet
stay the source of truth. When MLflow is unavailable (or a fake module is
injected), the manager degrades to the local JSONL audit trail and
continues to work without hosted services.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.engine import BacktestResult
from backtest.metrics import drawdown
from backtest.validation import BootstrapConfidenceInterval
from research.contracts import Experiment, ResearchInputError

from .gate import GateDecision, GateVerdict

try:  # Import lazily so research metrics remain usable without the MLflow extra.
    import mlflow as _mlflow
except Exception:  # pragma: no cover - depends on optional deployment installation
    _mlflow = None


class ExperimentTrackingError(ResearchInputError):
    """Raised when an experiment cannot be recorded or read."""


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """Auditable outcome of one accepted or rejected research experiment."""

    run_id: str
    experiment_id: str
    hypothesis_id: str
    strategy: str
    status: str
    commit_hash: str
    started_at: datetime
    ended_at: datetime
    metrics: Mapping[str, float | int]
    validation: Mapping[str, Any]
    benchmarks: Mapping[str, Mapping[str, float | int]]
    reason: str | None = None
    dataset_version: str | None = None
    cost_model: str | None = None
    backtest_period: str | None = None
    oos_period: str | None = None
    strategy_version: str = "1.0"
    factor_versions: Mapping[str, str] = field(default_factory=dict)
    validation_method: str | None = None
    random_seed: int | None = None
    dataset_fingerprint: str | None = None
    rebalance_frequency: str = "M"
    gate_result: Mapping[str, Any] | None = None
    artifacts: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible record."""
        return {
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "strategy": self.strategy,
            "status": self.status,
            "commit_hash": self.commit_hash,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "metrics": dict(self.metrics),
            "validation": dict(self.validation),
            "benchmarks": {key: dict(value) for key, value in self.benchmarks.items()},
            "reason": self.reason,
            "dataset_version": self.dataset_version,
            "cost_model": self.cost_model,
            "backtest_period": self.backtest_period,
            "oos_period": self.oos_period,
            "strategy_version": self.strategy_version,
            "factor_versions": dict(self.factor_versions),
            "validation_method": self.validation_method,
            "random_seed": self.random_seed,
            "dataset_fingerprint": self.dataset_fingerprint,
            "rebalance_frequency": self.rebalance_frequency,
            "gate_result": dict(self.gate_result) if self.gate_result else None,
            "artifacts": dict(self.artifacts) if self.artifacts else None,
        }


def _to_mapping(value: object | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        mapped = to_dict()
        if isinstance(mapped, Mapping):
            return dict(mapped)
    raise ExperimentTrackingError(
        "validation output must be a mapping or expose to_dict()"
    )


def _commit_hash() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return completed.stdout.strip() or "unknown"


def _safe_name(value: str) -> str:
    """Return a filesystem-safe slug for artifact names."""
    import re

    return re.sub(r"[^A-Za-z0-9_.-]", "_", value.strip())[:64] or "strategy"


def _write_dataframe(path: Path, frame: pd.DataFrame | pd.Series) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(frame, pd.Series):
        frame = frame.rename(frame.name or "value").to_frame()
    frame.to_csv(path)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _plotly_html(title: str, traces: list[tuple[str, pd.Series | pd.DataFrame]]) -> str:
    """Render a deterministic self-contained Plotly HTML figure.

    Plotly exports work without kaleido and the HTML is a valid artifact for
    MLflow or the reports directory.
    """
    import plotly.graph_objects as go

    figure = go.Figure()
    figure.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title="Value",
        template="plotly_white",
    )
    for name, series in traces:
        if isinstance(series, pd.DataFrame):
            for column in series.columns:
                figure.add_trace(
                    go.Scatter(
                        x=series.index,
                        y=series[column],
                        mode="lines",
                        name=f"{name}:{column}",
                    )
                )
        else:
            figure.add_trace(
                go.Scatter(x=series.index, y=series, mode="lines", name=name)
            )
    return figure.to_html(full_html=False, include_plotlyjs="cdn")


def build_research_artifacts(
    result: BacktestResult,
    *,
    artifact_dir: Path | str,
    experiment_id: str,
    benchmarks: Mapping[str, BacktestResult] | None = None,
    validation: Mapping[str, Any] | None = None,
    confidence_intervals: Mapping[str, BootstrapConfidenceInterval] | None = None,
    gate_result: GateDecision | Mapping[str, Any] | None = None,
    factor_diagnostics: Mapping[str, Any] | None = None,
    research_report: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Write the standard MLflow artifact set for one research run.

    Returns a ``{artifact_name: file_path}`` mapping suitable for
    ``ExperimentManager.log_experiment(artifacts=...)``. Every file is
    written deterministically from the supplied research outputs.
    """
    directory = Path(artifact_dir).expanduser() / _safe_name(experiment_id)
    directory.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}
    _write_dataframe(directory / "equity_curve.csv", result.equity_curve)
    artifacts["equity_curve.csv"] = str(directory / "equity_curve.csv")
    _write_dataframe(directory / "returns.csv", result.returns)
    artifacts["returns.csv"] = str(directory / "returns.csv")
    _write_dataframe(directory / "drawdown_series.csv", drawdown(result.equity_curve))
    artifacts["drawdown_series.csv"] = str(directory / "drawdown_series.csv")
    _write_dataframe(directory / "turnover.csv", result.trades["turnover"])
    artifacts["turnover.csv"] = str(directory / "turnover.csv")
    _write_dataframe(directory / "portfolio_weights.csv", result.weights)
    artifacts["portfolio_weights.csv"] = str(directory / "portfolio_weights.csv")
    if validation is not None:
        validation_payload = _to_mapping(validation)
        _write_json(directory / "validation.json", validation_payload)
        artifacts["validation.json"] = str(directory / "validation.json")
        folds = validation_payload.get("fold_metrics")
        if isinstance(folds, list) and folds:
            fold_frame = pd.DataFrame(
                folds,
                index=[f"fold_{index}" for index in range(len(folds))],
            )
            _write_dataframe(directory / "validation_fold_metrics.csv", fold_frame)
            artifacts["validation_fold_metrics.csv"] = str(
                directory / "validation_fold_metrics.csv"
            )
    if confidence_intervals:
        _write_json(
            directory / "confidence_intervals.json",
            {
                name: interval.to_dict()
                for name, interval in confidence_intervals.items()
            },
        )
        artifacts["confidence_intervals.json"] = str(
            directory / "confidence_intervals.json"
        )
    if gate_result is not None:
        gate_payload = (
            gate_result.to_dict()
            if isinstance(gate_result, GateDecision)
            else _to_mapping(gate_result)
        )
        _write_json(directory / "research_gate.json", gate_payload)
        artifacts["research_gate.json"] = str(directory / "research_gate.json")
    if factor_diagnostics:
        _write_json(directory / "factor_diagnostics.json", factor_diagnostics)
        artifacts["factor_diagnostics.json"] = str(
            directory / "factor_diagnostics.json"
        )
    html_path = directory / "drawdown_plot.html"
    html_path.write_text(
        _plotly_html(
            "Drawdown",
            [("drawdown", drawdown(result.equity_curve))],
        ),
        encoding="utf-8",
    )
    artifacts["drawdown_plot.html"] = str(html_path)
    validation_traces = [
        ("returns", result.returns),
        ("turnover", result.trades["turnover"]),
    ]
    if benchmarks:
        for name, benchmark in benchmarks.items():
            validation_traces.append((f"{name}_equity", benchmark.equity_curve))
    validation_html = directory / "validation_plot.html"
    validation_html.write_text(
        _plotly_html("Validation", validation_traces), encoding="utf-8"
    )
    artifacts["validation_plot.html"] = str(validation_html)
    if research_report is not None:
        report_payload = _to_mapping(research_report)
        _write_json(directory / "research_report.json", report_payload)
        artifacts["research_report.json"] = str(directory / "research_report.json")
        markdown = getattr(research_report, "to_markdown", None)
        if callable(markdown):
            markdown_path = directory / "research_report.md"
            markdown_path.write_text(str(markdown()), encoding="utf-8")
            artifacts["research_report.md"] = str(markdown_path)
    return artifacts


class ExperimentManager:
    """Log reproducible experiments to MLflow and a local JSONL audit trail.

    The manager never requires a hosted MLflow service: with no tracking URI
    it creates a local SQLite backend beneath ``tracking_dir``, and without
    MLflow installed it still records the full audit trail locally.
    """

    def __init__(
        self,
        experiment_name: str = "quant-india",
        tracking_uri: str | None = None,
        tracking_dir: Path | str = "reports/generated/experiments",
        *,
        mlflow_module: Any | None = None,
        logger: logging.Logger | None = None,
        minimum_deflated_sharpe_probability: float = 0.95,
    ) -> None:
        if not experiment_name.strip():
            raise ExperimentTrackingError("experiment_name must be non-empty")
        if not 0 < minimum_deflated_sharpe_probability < 1:
            raise ExperimentTrackingError(
                "minimum DSR probability must be between zero and one"
            )
        self.experiment_name = experiment_name.strip()
        self.tracking_uri = tracking_uri
        self.tracking_dir = Path(tracking_dir).expanduser()
        self.history_path = self.tracking_dir / "experiments.jsonl"
        self.mlflow = _mlflow if mlflow_module is None else mlflow_module
        self.logger = logger or logging.getLogger(__name__)
        self.minimum_deflated_sharpe_probability = minimum_deflated_sharpe_probability
        if self.mlflow is not None:
            if tracking_uri:
                uri = tracking_uri
            else:
                self.tracking_dir.mkdir(parents=True, exist_ok=True)
                database_path = (self.tracking_dir / "mlflow.db").resolve()
                uri = f"sqlite:///{database_path}"
            self.mlflow.set_tracking_uri(uri)

    @staticmethod
    def _numeric_metrics(metrics: Mapping[str, Any]) -> dict[str, float]:
        numeric: dict[str, float] = {}
        for key, value in metrics.items():
            if isinstance(value, bool):
                continue
            if isinstance(value, int | float) and math.isfinite(float(value)):
                numeric[key] = float(value)
        return numeric

    def _log_mlflow(
        self,
        experiment: Experiment,
        record: ExperimentRecord,
        artifacts: Mapping[str, str] | None,
    ) -> str:
        if self.mlflow is None:
            raise ExperimentTrackingError(
                "MLflow is not installed; install the research requirements "
                "before tracking"
            )
        try:
            self.mlflow.set_experiment(self.experiment_name)
            with self.mlflow.start_run(run_name=experiment.experiment_id) as run:
                run_id = str(run.info.run_id)
                self.mlflow.log_params(
                    {
                        "hypothesis_id": experiment.hypothesis_id,
                        "strategy": experiment.strategy,
                        "strategy_version": record.strategy_version,
                        "factor_versions": json.dumps(
                            dict(record.factor_versions), sort_keys=True
                        ),
                        "universe": experiment.universe,
                        "factor_set": ",".join(experiment.factor_set),
                        "rebalance_frequency": record.rebalance_frequency,
                        "cost_model": record.cost_model or "default",
                        "validation_method": record.validation_method or "unknown",
                        "random_seed": str(record.random_seed)
                        if record.random_seed is not None
                        else "unknown",
                        "git_commit": record.commit_hash,
                        "dataset_fingerprint": record.dataset_fingerprint or "unknown",
                        "dataset_version": record.dataset_version
                        or experiment.dataset_version
                        or "unknown",
                        **{
                            f"parameter_{key}": str(value)
                            for key, value in experiment.parameters.items()
                        },
                    }
                )
                metrics = self._numeric_metrics(record.metrics)
                if metrics:
                    self.mlflow.log_metrics(metrics)
                self.mlflow.set_tags(
                    {
                        "experiment_id": experiment.experiment_id,
                        "commit_hash": record.commit_hash,
                        "status": record.status,
                        "started_at": record.started_at.isoformat(),
                        "ended_at": record.ended_at.isoformat(),
                        "validation_json": json.dumps(record.validation, default=str),
                        "benchmarks_json": json.dumps(record.benchmarks, default=str),
                        "reason": record.reason or "",
                        "gate_result": json.dumps(
                            record.gate_result or {}, default=str, sort_keys=True
                        ),
                    }
                )
                if artifacts and hasattr(self.mlflow, "log_artifact"):
                    for name, path in sorted(artifacts.items()):
                        self.mlflow.log_artifact(path, artifact_path=name)
                return run_id
        except Exception as exc:
            raise ExperimentTrackingError("MLflow experiment logging failed") from exc

    def _append_local(self, record: ExperimentRecord) -> None:
        try:
            self.tracking_dir.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record.to_dict(), default=str, sort_keys=True) + "\n"
                )
        except OSError as exc:
            raise ExperimentTrackingError(
                "local experiment history could not be written"
            ) from exc

    def list_records(self) -> tuple[ExperimentRecord, ...]:
        """Return locally persisted experiment records in insertion order."""
        if not self.history_path.exists():
            return ()
        records: list[ExperimentRecord] = []
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ExperimentTrackingError(
                "local experiment history could not be read"
            ) from exc
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                records.append(
                    ExperimentRecord(
                        run_id=str(payload["run_id"]),
                        experiment_id=str(payload["experiment_id"]),
                        hypothesis_id=str(payload["hypothesis_id"]),
                        strategy=str(payload["strategy"]),
                        status=str(payload["status"]),
                        commit_hash=str(payload["commit_hash"]),
                        started_at=datetime.fromisoformat(payload["started_at"]),
                        ended_at=datetime.fromisoformat(payload["ended_at"]),
                        metrics=payload.get("metrics", {}),
                        validation=payload.get("validation", {}),
                        benchmarks=payload.get("benchmarks", {}),
                        reason=payload.get("reason"),
                        dataset_version=payload.get("dataset_version"),
                        cost_model=payload.get("cost_model"),
                        backtest_period=payload.get("backtest_period"),
                        oos_period=payload.get("oos_period"),
                        strategy_version=payload.get("strategy_version", "1.0"),
                        factor_versions=payload.get("factor_versions", {}) or {},
                        validation_method=payload.get("validation_method"),
                        random_seed=payload.get("random_seed"),
                        dataset_fingerprint=payload.get("dataset_fingerprint"),
                        rebalance_frequency=payload.get("rebalance_frequency", "M"),
                        gate_result=payload.get("gate_result"),
                        artifacts=payload.get("artifacts"),
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ExperimentTrackingError(
                    "local experiment history contains invalid JSON"
                ) from exc
        return tuple(records)

    def log_experiment(
        self,
        experiment: Experiment,
        *,
        result: Any | None = None,
        validation: Mapping[str, Any] | object | None = None,
        benchmarks: Mapping[str, Any] | None = None,
        rejected: bool = False,
        reason: str | None = None,
        dataset_version: str | None = None,
        cost_model: str | None = None,
        backtest_period: str | None = None,
        oos_period: str | None = None,
        artifacts: Mapping[str, str] | None = None,
        strategy_version: str = "1.0",
        factor_versions: Mapping[str, str] | None = None,
        validation_method: str | None = None,
        random_seed: int | None = None,
        dataset_fingerprint: str | None = None,
        git_commit: str | None = None,
        rebalance_frequency: str = "M",
        gate_result: GateDecision | Mapping[str, Any] | None = None,
    ) -> ExperimentRecord:
        """Log an experiment, including rejected trials, to MLflow and local history.

        ``validation`` should carry the statistical-validity payload (DSR,
        walk-forward, CPCV, bootstrap CIs); ``gate_result`` should carry the
        :class:`research.gate.GateDecision`. Both are stored as metadata;
        no experimental output is stored in MLflow as the source of truth.
        """
        started_at = datetime.now(UTC)
        metrics = _to_mapping(getattr(result, "metrics", None))
        validation_mapping = _to_mapping(validation)
        benchmark_metrics = {
            name: _to_mapping(getattr(benchmark, "metrics", benchmark))
            for name, benchmark in (benchmarks or {}).items()
        }
        if result is not None and hasattr(result, "returns"):
            from backtest.validation import deflated_sharpe_from_returns

            previous_trials = len(self.list_records())
            dsr = deflated_sharpe_from_returns(result.returns, previous_trials + 1)
            validation_mapping["deflated_sharpe"] = dsr.to_dict()
            metrics["deflated_sharpe_probability"] = dsr.probability
            if dsr.probability < self.minimum_deflated_sharpe_probability:
                rejected = True
                reason = (
                    reason or "deflated Sharpe probability below acceptance threshold"
                )
        gate_payload = (
            gate_result.to_dict()
            if isinstance(gate_result, GateDecision)
            else _to_mapping(gate_result)
        )
        if gate_payload:
            metrics["gate_score"] = float(gate_payload.get("score", 0.0))
            metrics["gate_verdict"] = _verdict_numeric(gate_payload.get("verdict"))
        status = "rejected" if rejected else "accepted"
        record = ExperimentRecord(
            run_id="pending",
            experiment_id=experiment.experiment_id,
            hypothesis_id=experiment.hypothesis_id,
            strategy=experiment.strategy,
            status=status,
            commit_hash=git_commit or _commit_hash(),
            started_at=started_at,
            ended_at=datetime.now(UTC),
            metrics={
                key: value
                for key, value in metrics.items()
                if isinstance(value, int | float)
            },
            validation=validation_mapping,
            benchmarks=benchmark_metrics,
            reason=reason,
            dataset_version=dataset_version or experiment.dataset_version,
            cost_model=cost_model or experiment.cost_model,
            backtest_period=backtest_period,
            oos_period=oos_period,
            strategy_version=strategy_version,
            factor_versions=dict(factor_versions or {}),
            validation_method=validation_method,
            random_seed=random_seed,
            dataset_fingerprint=dataset_fingerprint,
            rebalance_frequency=rebalance_frequency,
            gate_result=gate_payload or None,
            artifacts=dict(artifacts) if artifacts else None,
        )
        run_id = (
            self._log_mlflow(experiment, record, artifacts)
            if self.mlflow is not None
            else "local"
        )
        record = replace(record, run_id=run_id)
        self._append_local(record)
        self.logger.info(
            "experiment_logged",
            extra={
                "operation": "log_experiment",
                "experiment_id": record.experiment_id,
                "status": record.status,
            },
        )
        return record


def _verdict_numeric(verdict: object | None) -> float:
    if isinstance(verdict, GateVerdict):
        verdict = verdict.value
    return {
        GateVerdict.PASS.value: 4.0,
        GateVerdict.FRAGILE.value: 3.0,
        GateVerdict.FAIL.value: 2.0,
        GateVerdict.INSUFFICIENT_EVIDENCE.value: 1.0,
    }.get(str(verdict), 0.0)
