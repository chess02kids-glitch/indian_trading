"""MLflow-backed experiment tracking with an auditable local history."""

from __future__ import annotations

import json
import logging
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Experiment, ResearchInputError

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
    raise ExperimentTrackingError("validation output must be a mapping or expose to_dict()")


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


class ExperimentManager:
    """Log reproducible experiments to MLflow and a local JSONL audit trail."""

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
            raise ExperimentTrackingError("minimum DSR probability must be between zero and one")
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

    def _log_mlflow(self, experiment: Experiment, record: ExperimentRecord) -> str:
        if self.mlflow is None:
            raise ExperimentTrackingError(
                "MLflow is not installed; install the research requirements before tracking"
            )
        try:
            self.mlflow.set_experiment(self.experiment_name)
            with self.mlflow.start_run(run_name=experiment.experiment_id) as run:
                run_id = str(run.info.run_id)
                self.mlflow.log_params(
                    {
                        "hypothesis_id": experiment.hypothesis_id,
                        "strategy": experiment.strategy,
                        "universe": experiment.universe,
                        "factor_set": ",".join(experiment.factor_set),
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
                    }
                )
                return run_id
        except Exception as exc:
            raise ExperimentTrackingError("MLflow experiment logging failed") from exc

    def _append_local(self, record: ExperimentRecord) -> None:
        try:
            self.tracking_dir.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), default=str, sort_keys=True) + "\n")
        except OSError as exc:
            raise ExperimentTrackingError("local experiment history could not be written") from exc

    def list_records(self) -> tuple[ExperimentRecord, ...]:
        """Return locally persisted experiment records in insertion order."""
        if not self.history_path.exists():
            return ()
        records: list[ExperimentRecord] = []
        try:
            lines = self.history_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ExperimentTrackingError("local experiment history could not be read") from exc
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
    ) -> ExperimentRecord:
        """Log an experiment, including rejected trials, to MLflow and local history."""
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
                reason = reason or "deflated Sharpe probability below acceptance threshold"
        status = "rejected" if rejected else "accepted"
        record = ExperimentRecord(
            run_id="pending",
            experiment_id=experiment.experiment_id,
            hypothesis_id=experiment.hypothesis_id,
            strategy=experiment.strategy,
            status=status,
            commit_hash=_commit_hash(),
            started_at=started_at,
            ended_at=datetime.now(UTC),
            metrics={
                key: value for key, value in metrics.items() if isinstance(value, int | float)
            },
            validation=validation_mapping,
            benchmarks=benchmark_metrics,
            reason=reason,
        )
        run_id = self._log_mlflow(experiment, record) if self.mlflow is not None else "local"
        record = ExperimentRecord(
            run_id=run_id,
            experiment_id=record.experiment_id,
            hypothesis_id=record.hypothesis_id,
            strategy=record.strategy,
            status=record.status,
            commit_hash=record.commit_hash,
            started_at=record.started_at,
            ended_at=record.ended_at,
            metrics=record.metrics,
            validation=record.validation,
            benchmarks=record.benchmarks,
            reason=record.reason,
        )
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
