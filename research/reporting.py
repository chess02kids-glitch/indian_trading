"""Machine-readable and human-readable research report generation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import sqrt
from pathlib import Path
from typing import Any

import pandas as pd

from backtest.benchmarks import compare_results
from backtest.engine import BacktestResult
from backtest.metrics import drawdown, rolling_sharpe

from .contracts import ResearchInputError

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")

_PERIOD_LABELS = {"D": "daily", "W": "weekly", "M": "monthly"}


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
    metadata: Mapping[str, Any]

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
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        """Serialize the report as deterministic JSON."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), default=str
        )

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
        lines.extend(
            ["", "## Benchmark comparison", "", "| Name | Metrics |", "| --- | --- |"]
        )
        for name, metrics in self.benchmark_comparison.items():
            lines.append(
                f"| {name} | `{json.dumps(metrics, sort_keys=True, default=str)}` |"
            )
        lines.extend(["", "## Allocation summary", ""])
        allocation_json = json.dumps(self.allocation_summary, indent=2, default=str)
        lines.append(f"```json\n{allocation_json}\n```")
        if self.validation:
            validation_json = json.dumps(self.validation, indent=2, default=str)
            lines.extend(["", "## Validation", "", f"```json\n{validation_json}\n```"])
        if self.metadata:
            metadata_json = json.dumps(
                self.metadata, indent=2, sort_keys=True, default=str
            )
            lines.extend(
                [
                    "",
                    "## Reproducibility metadata",
                    "",
                    f"```json\n{metadata_json}\n```",
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


@dataclass(frozen=True, slots=True)
class PeriodReport:
    """One aggregation period (daily/weekly/monthly) of a backtest.

    Captures the portfolio-reporting essentials: exposure, turnover,
    drawdown, and (optionally) factor exposure of held positions.
    """

    strategy: str
    period: str
    label: str
    periods: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Return the full machine-readable period report."""
        return {
            "strategy": self.strategy,
            "period": self.period,
            "label": self.label,
            "periods": list(self.periods),
        }

    def to_json(self) -> str:
        """Serialize as deterministic JSON."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), default=str
        )

    def write(self, output_dir: Path | str = "reports/generated") -> Path:
        """Write the period report to JSON beneath ``output_dir``."""
        directory = Path(output_dir).expanduser()
        stem = _SAFE_NAME.sub("_", self.strategy.strip()) or "strategy"
        path = directory / f"{stem}_report_{self.period}.json"
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json() + "\n", encoding="utf-8")
        return path


def generate_periodic_reports(
    result: BacktestResult,
    *,
    factor_values: pd.DataFrame | None = None,
    periods: Sequence[str] = ("D", "W", "M"),
) -> dict[str, PeriodReport]:
    """Aggregate a backtest into daily/weekly/monthly portfolio reports.

    Each period row reports ``period_return``, ``cumulative_return``,
    ``volatility``, ``sharpe``, ``max_drawdown``, ``exposure`` (mean gross
    weight), ``max_holding``, ``num_holdings``, ``turnover``, and — when
    ``factor_values`` is supplied — ``factor_exposure`` (mean factor score
    across held positions at the period end).
    """
    if result.returns.empty:
        raise ResearchInputError("cannot build period reports from empty returns")
    unknown = set(periods) - set(_PERIOD_LABELS)
    if unknown:
        raise ResearchInputError(f"unsupported report periods: {sorted(unknown)}")
    if factor_values is not None and (
        not factor_values.index.equals(result.returns.index)
        or not factor_values.columns.equals(result.weights.columns)
    ):
        raise ResearchInputError(
            "factor_values must align with the backtest returns and weights"
        )

    equity = result.equity_curve
    drawdowns = drawdown(equity)
    cumulative = equity / equity.iloc[0] - 1.0
    output: dict[str, PeriodReport] = {}
    for period in periods:
        groups = result.returns.index.to_period(period)
        rows: list[dict[str, Any]] = []
        for key, indices in result.returns.groupby(groups).groups.items():
            index = pd.DatetimeIndex(indices).sort_values()
            period_returns = result.returns.loc[index]
            period_return = float((1.0 + period_returns).prod() - 1.0)
            period_vol = float(period_returns.std(ddof=1) * sqrt(252))
            sharpe = (
                float(period_returns.mean() / period_returns.std(ddof=1) * sqrt(252))
                if period_returns.std(ddof=1) > 0
                else 0.0
            )
            weights = result.weights.loc[index]
            turnover = float(result.trades.loc[index, "turnover"].sum())
            exposure = float(weights.sum(axis=1).mean()) if not weights.empty else 0.0
            row: dict[str, Any] = {
                "period_start": index[0].date().isoformat(),
                "period_end": index[-1].date().isoformat(),
                "period_return": period_return,
                "cumulative_return": float(cumulative.loc[index[-1]]),
                "volatility": period_vol,
                "sharpe": sharpe,
                "max_drawdown": float(drawdowns.loc[index].min()),
                "exposure": exposure,
                "max_holding": float(weights.abs().sum(axis=1).max())
                if not weights.empty
                else 0.0,
                "num_holdings": int((weights.iloc[-1] > 0).sum())
                if not weights.empty
                else 0,
                "turnover": turnover,
            }
            if factor_values is not None:
                held = weights.loc[index[-1]] > 0
                row["factor_exposure"] = (
                    float(factor_values.loc[index[-1], held].mean())
                    if held.any()
                    else 0.0
                )
            rows.append(row)
        output[period] = PeriodReport(
            strategy=result.strategy_name,
            period=period,
            label=_PERIOD_LABELS[period],
            periods=rows,
        )
    return output


def generate_report(
    result: BacktestResult,
    benchmark_results: Mapping[str, BacktestResult] | None = None,
    validation: object | None = None,
    *,
    rolling_window: int = 63,
    metadata: Mapping[str, Any] | None = None,
) -> ResearchReport:
    """Generate standardized series, allocation, benchmark, and validation output.

    ``metadata`` carries the experiment-specific reproducibility context such
    as strategy parameters, historical universe, dataset version, and seed.
    Engine and cost metadata is always retained from ``result``.
    """
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
        rolling_sharpes=_series_records(
            rolling_sharpe(result.returns, window=rolling_window)
        ),
        turnover=_series_records(result.trades["turnover"]),
        allocation_summary=allocation,
        benchmark_comparison=comparison.to_dict(orient="index"),
        validation=_mapping(validation),
        metadata={**result.metadata, **(dict(metadata) if metadata else {})},
    )
