"""Machine-readable and human-readable research report generation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.benchmarks import compare_results
from backtest.engine import BacktestResult
from backtest.metrics import drawdown, rolling_sharpe

from .contracts import ResearchInputError

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _series_records(series: pd.Series) -> list[dict[str, Any]]:
    return [
        {
            "date": index.isoformat(),
            "value": None if pd.isna(value) else float(value),
        }
        for index, value in series.items()
    ]


def _mapping(value: object | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        output = to_dict()
        if isinstance(output, Mapping):
            return dict(output)
    raise ResearchInputError("validation output must be a mapping or expose to_dict()")


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """Complete report payload for one strategy and its benchmarks."""

    strategy: str
    generated_at: datetime
    metrics: Mapping[str, Any]
    cumulative_returns: list[dict[str, Any]]
    drawdowns: list[dict[str, Any]]
    rolling_sharpes: list[dict[str, Any]]
    turnover: list[dict[str, Any]]
    allocation_summary: Mapping[str, Any]
    benchmark_comparison: Mapping[str, Mapping[str, Any]]
    validation: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the full machine-readable report."""
        return {
            "strategy": self.strategy,
            "generated_at": self.generated_at.isoformat(),
            "metrics": dict(self.metrics),
            "cumulative_returns": list(self.cumulative_returns),
            "drawdowns": list(self.drawdowns),
            "rolling_sharpes": list(self.rolling_sharpes),
            "turnover": list(self.turnover),
            "allocation_summary": dict(self.allocation_summary),
            "benchmark_comparison": {
                key: dict(value) for key, value in self.benchmark_comparison.items()
            },
            "validation": dict(self.validation),
        }

    def to_json(self) -> str:
        """Serialize the report as deterministic JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str)

    def to_markdown(self) -> str:
        """Render a concise human-readable report."""
        lines = [
            f"# Research report: {self.strategy}",
            "",
            f"Generated: `{self.generated_at.isoformat()}`",
            "",
            "## Performance",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
        for name, value in self.metrics.items():
            lines.append(f"| {name} | {value} |")
        lines.extend(["", "## Benchmark comparison", "", "| Name | Metrics |", "| --- | --- |"])
        for name, metrics in self.benchmark_comparison.items():
            lines.append(f"| {name} | `{json.dumps(metrics, sort_keys=True, default=str)}` |")
        lines.extend(["", "## Allocation summary", ""])
        lines.append(f"```json\n{json.dumps(self.allocation_summary, indent=2, default=str)}\n```")
        if self.validation:
            lines.extend(
                [
                    "",
                    "## Validation",
                    "",
                    f"```json\n{json.dumps(self.validation, indent=2, default=str)}\n```",
                ]
            )
        return "\n".join(lines) + "\n"

    def write(self, output_dir: Path | str = "reports/generated") -> tuple[Path, Path]:
        """Write JSON and Markdown reports beneath ``output_dir``."""
        directory = Path(output_dir).expanduser()
        stem = _SAFE_NAME.sub("_", self.strategy.strip()) or "strategy"
        json_path = directory / f"{stem}.json"
        markdown_path = directory / f"{stem}.md"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            json_path.write_text(self.to_json() + "\n", encoding="utf-8")
            markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        except OSError as exc:
            raise ResearchInputError(
                f"research report could not be written to {directory}"
            ) from exc
        return json_path, markdown_path


def generate_report(
    result: BacktestResult,
    benchmark_results: Mapping[str, BacktestResult] | None = None,
    validation: object | None = None,
    *,
    rolling_window: int = 63,
) -> ResearchReport:
    """Generate standardized series, allocation, benchmark, and validation output."""
    if rolling_window < 2:
        raise ResearchInputError("rolling_window must be at least two")
    cumulative = result.equity_curve / result.equity_curve.iloc[0] - 1.0
    all_results = {result.strategy_name: result, **(benchmark_results or {})}
    comparison = compare_results(all_results)
    allocation = {
        "average_weights": result.weights.mean().to_dict(),
        "final_weights": result.weights.iloc[-1].to_dict(),
        "turnover_total": float(result.trades["turnover"].sum()),
        "rebalance_count": int(result.trades["rebalance"].sum()),
    }
    return ResearchReport(
        strategy=result.strategy_name,
        generated_at=datetime.now(UTC),
        metrics=result.metrics.to_dict(),
        cumulative_returns=_series_records(cumulative),
        drawdowns=_series_records(drawdown(result.equity_curve)),
        rolling_sharpes=_series_records(rolling_sharpe(result.returns, window=rolling_window)),
        turnover=_series_records(result.trades["turnover"]),
        allocation_summary=allocation,
        benchmark_comparison=comparison.to_dict(orient="index"),
        validation=_mapping(validation),
    )
