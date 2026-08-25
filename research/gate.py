"""Automated research gate: the decision layer between backtest and paper.

The gate answers one question — *does this strategy deserve paper-trading
capital?* — and it can only answer exactly one of::

    PASS
    FAIL
    FRAGILE
    INSUFFICIENT_EVIDENCE

Every decision carries an explicit, auditable set of reasons. There are no
silent approvals: a strategy can only reach ``PASS`` when every configured
check passes, and the gate always records the evidence (metrics,
confidence intervals, benchmark comparison, and validation consistency)
that produced the verdict.

The gate compares the candidate against Buy & Hold, Equal Weight, Inverse
Volatility, Persistence, and a seeded Random Placebo family, and evaluates
statistical confidence, cost robustness, drawdown, turnover, and validation
consistency.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from math import isfinite, sqrt
from typing import Any

import numpy as np
import pandas as pd

from backtest.benchmarks import random_weights
from backtest.engine import BacktestResult, VectorBTResearchEngine
from backtest.metrics import PerformanceMetrics
from backtest.validation import (
    BootstrapConfidenceInterval,
    CrossValidationResult,
    WalkForwardResult,
    bootstrap_metric_intervals,
    validation_consistency,
)
from research.contracts import ResearchInputError

__all__ = [
    "GateCheck",
    "GateDecision",
    "GateVerdict",
    "ResearchGate",
    "ResearchGateConfig",
    "generate_placebo_results",
]


class GateVerdict(str, Enum):
    """The only four outcomes the research gate may produce."""

    PASS = "PASS"
    FAIL = "FAIL"
    FRAGILE = "FRAGILE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One gate check with its status, human-readable reason, and evidence."""

    name: str
    status: str  # one of: pass, warn, fail
    message: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ("pass", "warn", "fail"):
            raise ResearchInputError("gate check status must be pass, warn, or fail")
        if not self.name.strip() or not self.message.strip():
            raise ResearchInputError("gate check name and message are required")

    def to_dict(self) -> dict[str, Any]:
        """Return a report-ready check mapping."""
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Complete, self-explanatory decision produced by :class:`ResearchGate`."""

    verdict: str
    score: float
    checks: tuple[GateCheck, ...]
    metrics: Mapping[str, Any]
    benchmarks: Mapping[str, Mapping[str, Any]]
    confidence_intervals: Mapping[str, Mapping[str, Any]]
    reproducibility: Mapping[str, Any]
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.verdict not in {item.value for item in GateVerdict}:
            raise ResearchInputError(f"unknown gate verdict: {self.verdict}")
        if not 0.0 <= self.score <= 100.0:
            raise ResearchInputError("gate score must be within [0, 100]")

    def to_dict(self) -> dict[str, Any]:
        """Return the full machine-readable decision."""
        return {
            "verdict": self.verdict,
            "score": self.score,
            "checks": [check.to_dict() for check in self.checks],
            "metrics": dict(self.metrics),
            "benchmarks": {
                name: dict(metrics) for name, metrics in self.benchmarks.items()
            },
            "confidence_intervals": {
                name: dict(interval)
                for name, interval in self.confidence_intervals.items()
            },
            "reproducibility": dict(self.reproducibility),
            "generated_at": self.generated_at.isoformat(),
        }

    @property
    def failures(self) -> tuple[GateCheck, ...]:
        """Return checks that failed (the reason the gate did not approve)."""
        return tuple(check for check in self.checks if check.status == "fail")

    @property
    def warnings(self) -> tuple[GateCheck, ...]:
        """Return checks that passed only with a warning."""
        return tuple(check for check in self.checks if check.status == "warn")

    @property
    def reasons(self) -> tuple[str, ...]:
        """Human-readable reasons for the verdict, one per non-pass check."""
        return tuple(
            f"[{check.status}] {check.name}: {check.message}" for check in self.checks
        )

    def to_markdown(self) -> str:
        """Render a concise human-readable gate report."""
        lines = [
            f"# Research gate: {self.verdict}",
            "",
            f"Score: `{self.score:.1f}`",
            "",
            "## Checks",
            "",
            "| Status | Check | Reason |",
            "| --- | --- | --- |",
        ]
        for check in self.checks:
            lines.append(f"| {check.status} | {check.name} | {check.message} |")
        lines.extend(["", "## Metrics", "", "| Metric | Value |", "| --- | ---: |"])
        for name, value in self.metrics.items():
            lines.append(f"| {name} | {value} |")
        lines.extend(["", "## Confidence intervals", ""])
        for name, interval in self.confidence_intervals.items():
            lines.append(
                f"- **{name}**: estimate {interval.get('estimate')} "
                f"[{interval.get('lower')}, {interval.get('upper')}] "
                f"({interval.get('confidence')})"
            )
        lines.extend(["", "## Benchmark comparison", ""])
        for name, metrics in self.benchmarks.items():
            lines.append(
                f"- **{name}**: Sharpe {metrics.get('sharpe')}, "
                f"CAGR {metrics.get('annualized_return')}, "
                f"max drawdown {metrics.get('max_drawdown')}"
            )
        return "\n".join(lines) + "\n"

    def write(self, output_dir: str | Any) -> Any:
        """Write the decision as JSON beneath ``output_dir``."""
        import json
        from pathlib import Path

        directory = Path(output_dir).expanduser()
        path = directory / "research_gate.json"
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return path


@dataclass(frozen=True, slots=True)
class ResearchGateConfig:
    """Thresholds and tolerances for the research gate.

    All values are conservative by default: the gate is a *barrier*, not a
    scoring tool. Lower thresholds only ever make the gate stricter.
    """

    minimum_observations: int = 252
    dsr_min_probability: float = 0.95
    sharpe_ci_min_lower: float = 0.0
    min_benchmark_win_rate: float = 0.6
    max_drawdown_limit: float = -0.30
    max_turnover_multiple: float = 8.0
    max_cost_drag_fraction: float = 0.5
    cost_stress_multiple: float = 2.0
    placebo_percentile: float = 0.95
    min_positive_fold_fraction: float = 0.5
    tested_variants: int | None = None

    def __post_init__(self) -> None:
        if self.minimum_observations < 2:
            raise ResearchInputError("minimum_observations must be at least two")
        if not 0 < self.dsr_min_probability <= 1:
            raise ResearchInputError("dsr_min_probability must be in (0, 1]")
        if not 0 <= self.min_benchmark_win_rate <= 1:
            raise ResearchInputError("min_benchmark_win_rate must be in [0, 1]")
        if self.max_drawdown_limit >= 0:
            raise ResearchInputError("max_drawdown_limit must be negative")
        if self.max_turnover_multiple < 0:
            raise ResearchInputError("max_turnover_multiple cannot be negative")
        if not 0 <= self.max_cost_drag_fraction <= 1:
            raise ResearchInputError("max_cost_drag_fraction must be in [0, 1]")
        if self.cost_stress_multiple < 1:
            raise ResearchInputError("cost_stress_multiple must be at least one")
        if not 0 <= self.placebo_percentile <= 1:
            raise ResearchInputError("placebo_percentile must be in [0, 1]")
        if not 0 <= self.min_positive_fold_fraction <= 1:
            raise ResearchInputError("min_positive_fold_fraction must be in [0, 1]")


def _annualized_turnover(turnover: float, observations: int, periods: int) -> float:
    return turnover * periods / observations if observations > 0 else 0.0


def _metrics_dict(metrics: PerformanceMetrics) -> dict[str, Any]:
    return metrics.to_dict()


def generate_placebo_results(
    prices: pd.DataFrame,
    *,
    engine: VectorBTResearchEngine,
    samples: int = 50,
    seed: int = 42,
) -> dict[str, BacktestResult]:
    """Run ``samples`` seeded random portfolios as a placebo family.

    Every placebo uses the same engine, cost model, and rebalance frequency
    as the candidate. The results are deterministic for a fixed ``seed``
    and keyed ``placebo_00001`` .. ``placebo_N``.
    """
    if samples < 1 or samples > 10_000:
        raise ResearchInputError("placebo samples must be between one and 10000")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ResearchInputError("seed must be an integer")
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ResearchInputError("prices must be a non-empty DataFrame")
    output: dict[str, BacktestResult] = {}
    for number in range(samples):
        weights = random_weights(prices, seed=seed + number)
        output[f"placebo_{number:05d}"] = engine.run(
            prices,
            weights,
            strategy_name=f"placebo_{number:05d}",
            universe_history=[],
        )
    return output


class ResearchGate:
    """Deterministic decision layer between research and paper trading.

    ``evaluate`` is a pure function of its inputs: the candidate backtest
    result, its benchmarks, optional placebo family, optional walk-forward
    / CPCV validation, and reproducibility metadata. It never touches
    execution, brokers, or the network.
    """

    def __init__(
        self,
        config: ResearchGateConfig | None = None,
        *,
        random_seed: int = 42,
        git_commit: str = "unknown",
        dataset_fingerprint: str = "unknown",
    ) -> None:
        self.config = config or ResearchGateConfig()
        self.random_seed = random_seed
        self.git_commit = git_commit
        self.dataset_fingerprint = dataset_fingerprint

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _annualized_sharpe(returns: pd.Series, periods: int = 252) -> float:
        deviation = returns.std(ddof=1)
        if deviation == 0 or not isfinite(deviation):
            return 0.0
        return float(returns.mean() / deviation * sqrt(periods))

    @staticmethod
    def _gross_returns(result: BacktestResult) -> pd.Series:
        if "total_cost" not in result.trades.columns:
            raise ResearchInputError("result.trades must contain a total_cost column")
        return result.returns + result.trades["total_cost"].astype(float)

    def _bootstrap_intervals(
        self, returns: pd.Series, turnover: pd.Series
    ) -> dict[str, BootstrapConfidenceInterval]:
        return bootstrap_metric_intervals(
            returns,
            turnover=turnover,
            samples=500,
            confidence=0.95,
            seed=self.random_seed,
            periods_per_year=252,
        )

    # -- main API ------------------------------------------------------------

    def evaluate(
        self,
        result: BacktestResult,
        *,
        benchmarks: Mapping[str, BacktestResult],
        validation: WalkForwardResult
        | CrossValidationResult
        | Mapping[str, Any]
        | None = None,
        placebo_results: Mapping[str, BacktestResult] | None = None,
        oos_returns: pd.Series | None = None,
        cost_model_name: str = "default",
        rebalance_frequency: str = "M",
        validation_method: str = "walk_forward",
        strategy_version: str = "unknown",
        factor_versions: Mapping[str, str] | None = None,
        universe: str = "unknown",
        generated_at: datetime | None = None,
    ) -> GateDecision:
        """Evaluate one candidate strategy and return a self-explanatory verdict.

        Parameters mirror the experiment reproducibility record so the gate
        decision can always be traced back to a specific run.
        """
        config = self.config
        returns = result.returns
        if returns.empty:
            raise ResearchInputError("cannot gate an empty backtest result")
        evidence_returns = oos_returns if oos_returns is not None else returns
        if evidence_returns.empty:
            raise ResearchInputError("out-of-sample returns may not be empty")

        trials = config.tested_variants or (
            len(benchmarks) + len(placebo_results or {}) + 1
        )
        checks: list[GateCheck] = []
        metrics: dict[str, Any] = {}

        # 1) evidence sufficiency -------------------------------------------------
        evidence_sufficient = len(evidence_returns) >= config.minimum_observations
        checks.append(
            GateCheck(
                name="evidence_sufficiency",
                status="pass" if evidence_sufficient else "fail",
                message=(
                    f"out-of-sample evidence has {len(evidence_returns)} "
                    f"observations (minimum {config.minimum_observations})"
                ),
                evidence={
                    "observations": len(evidence_returns),
                    "minimum": config.minimum_observations,
                },
            )
        )
        if not evidence_sufficient:
            return GateDecision(
                verdict=GateVerdict.INSUFFICIENT_EVIDENCE.value,
                score=0.0,
                checks=tuple(checks),
                metrics={},
                benchmarks={
                    name: _metrics_dict(benchmark.metrics)
                    for name, benchmark in benchmarks.items()
                },
                confidence_intervals={},
                reproducibility=self._reproducibility(
                    result,
                    strategy_version=strategy_version,
                    factor_versions=dict(factor_versions or {}),
                    universe=universe,
                    cost_model_name=cost_model_name,
                    rebalance_frequency=rebalance_frequency,
                    validation_method=validation_method,
                ),
                generated_at=generated_at or datetime.now(UTC),
            )

        # 2) statistical confidence ------------------------------------------------
        candidate_sharpe = self._annualized_sharpe(evidence_returns)
        from backtest.validation import deflated_sharpe_from_returns

        dsr = deflated_sharpe_from_returns(evidence_returns, trials)
        intervals = self._bootstrap_intervals(
            evidence_returns, result.trades["turnover"].loc[evidence_returns.index]
        )
        sharpe_ci = intervals["sharpe"]
        confidence_ok = (
            dsr.probability >= config.dsr_min_probability
            and sharpe_ci.lower > config.sharpe_ci_min_lower
        )
        checks.append(
            GateCheck(
                name="statistical_confidence",
                status="pass" if confidence_ok else "fail",
                message=(
                    f"deflated Sharpe probability {dsr.probability:.4f} "
                    f"(>= {config.dsr_min_probability}) and 95% Sharpe CI "
                    f"lower bound {sharpe_ci.lower:.4f} (>{config.sharpe_ci_min_lower})"
                ),
                evidence={
                    "deflated_sharpe_probability": dsr.probability,
                    "sharpe_ci_lower": sharpe_ci.lower,
                    "sharpe_ci_upper": sharpe_ci.upper,
                    "trials": trials,
                    "observations": len(evidence_returns),
                },
            )
        )
        metrics.update(
            {
                "sharpe": candidate_sharpe,
                "deflated_sharpe_probability": dsr.probability,
                "sharpe_ci_lower": sharpe_ci.lower,
                "sharpe_ci_upper": sharpe_ci.upper,
                "trials_corrected": trials,
            }
        )

        # 3) benchmark competitiveness -----------------------------------------------
        benchmark_metrics = {
            name: _metrics_dict(benchmark.metrics)
            for name, benchmark in benchmarks.items()
        }
        candidate_metrics = _metrics_dict(result.metrics)
        benchmark_sharpes = [
            float(entry["sharpe"]) for entry in benchmark_metrics.values()
        ]
        beaten = sum(1 for value in benchmark_sharpes if candidate_sharpe > value)
        win_rate = beaten / len(benchmark_sharpes) if benchmark_sharpes else 0.0
        benchmark_ok = win_rate >= config.min_benchmark_win_rate
        checks.append(
            GateCheck(
                name="benchmark_competitiveness",
                status="pass" if benchmark_ok else "fail",
                message=(
                    f"candidate Sharpe {candidate_sharpe:.4f} beats "
                    f"{win_rate:.0%} of {len(benchmark_sharpes)} benchmarks "
                    f"(minimum {config.min_benchmark_win_rate:.0%})"
                ),
                evidence={
                    "candidate_sharpe": candidate_sharpe,
                    "benchmark_sharpes": {
                        name: float(m["sharpe"])
                        for name, m in benchmark_metrics.items()
                    },
                    "win_rate": win_rate,
                },
            )
        )

        # 4) cost robustness ----------------------------------------------------------
        gross_returns = self._gross_returns(result)
        gross_total = float((1.0 + gross_returns).prod() - 1.0)
        net_total = float((1.0 + returns).prod() - 1.0)
        cost_drag = max(0.0, gross_total - net_total)
        cost_share = cost_drag / gross_total if gross_total > 0 else 0.0
        cost_series = result.trades["total_cost"].astype(float)
        stressed_returns = gross_returns - config.cost_stress_multiple * cost_series
        stressed_sharpe = self._annualized_sharpe(stressed_returns)
        cost_ok = cost_share <= config.max_cost_drag_fraction and stressed_sharpe > 0
        checks.append(
            GateCheck(
                name="cost_robustness",
                status="pass" if cost_ok else "fail",
                message=(
                    f"cost drag consumes {cost_share:.0%} of gross return "
                    f"(limit {config.max_cost_drag_fraction:.0%}); "
                    f"{config.cost_stress_multiple:.1f}x-cost Sharpe "
                    f"{stressed_sharpe:.4f}"
                ),
                evidence={
                    "cost_drag": cost_drag,
                    "gross_return": gross_total,
                    "net_return": net_total,
                    "cost_share": cost_share,
                    "stressed_sharpe": stressed_sharpe,
                },
            )
        )

        # 5) drawdown control ---------------------------------------------------------
        max_drawdown = float(result.metrics.max_drawdown)
        drawdown_ok = max_drawdown >= config.max_drawdown_limit
        checks.append(
            GateCheck(
                name="drawdown_control",
                status="pass" if drawdown_ok else "fail",
                message=(
                    f"maximum drawdown {max_drawdown:.2%} within limit "
                    f"{config.max_drawdown_limit:.2%}"
                ),
                evidence={
                    "max_drawdown": max_drawdown,
                    "limit": config.max_drawdown_limit,
                },
            )
        )

        # 6) turnover control ---------------------------------------------------------
        annual_turnover = _annualized_turnover(
            float(result.metrics.turnover), len(returns), 252
        )
        turnover_ok = annual_turnover <= config.max_turnover_multiple
        checks.append(
            GateCheck(
                name="turnover_control",
                status="pass" if turnover_ok else "fail",
                message=(
                    f"annualized turnover {annual_turnover:.2f}x within limit "
                    f"{config.max_turnover_multiple:.2f}x"
                ),
                evidence={
                    "annual_turnover": annual_turnover,
                    "total_turnover": result.metrics.turnover,
                    "limit": config.max_turnover_multiple,
                },
            )
        )

        # 7) validation consistency -----------------------------------------------------
        if validation is None:
            checks.append(
                GateCheck(
                    name="validation_consistency",
                    status="warn",
                    message="no walk-forward/CPCV validation evidence supplied",
                    evidence={},
                )
            )
        else:
            if isinstance(validation, (WalkForwardResult, CrossValidationResult)):
                consistency = validation_consistency(
                    validation, min_positive_sharpe=0.0
                )
            else:
                consistency = _mapping_consistency(validation)
            consistency_ok = (
                consistency["positive_fold_fraction"]
                >= config.min_positive_fold_fraction
            )
            checks.append(
                GateCheck(
                    name="validation_consistency",
                    status="pass" if consistency_ok else "fail",
                    message=(
                        f"{consistency['positive_fold_fraction']:.0%} of "
                        f"{consistency['folds']} folds have positive Sharpe "
                        f"(minimum {config.min_positive_fold_fraction:.0%})"
                    ),
                    evidence={
                        "positive_fold_fraction": consistency["positive_fold_fraction"],
                        "folds": consistency["folds"],
                        "worst_fold_sharpe": consistency.get("worst_fold_sharpe", 0.0),
                        "aggregate_dsr": consistency.get(
                            "aggregate_deflated_sharpe_probability", 0.0
                        ),
                    },
                )
            )

        # 8) placebo dominance -----------------------------------------------------------
        placebo_sharpes: dict[str, float] = {}
        if placebo_results:
            for name, placebo in placebo_results.items():
                placebo_sharpes[name] = self._annualized_sharpe(placebo.returns)
        if not placebo_sharpes:
            checks.append(
                GateCheck(
                    name="placebo_dominance",
                    status="warn",
                    message="no placebo family supplied; dominance not assessed",
                    evidence={},
                )
            )
        else:
            percentile = float(
                np.quantile(
                    np.array(list(placebo_sharpes.values()), dtype=float),
                    config.placebo_percentile,
                )
            )
            placebo_ok = candidate_sharpe > percentile
            checks.append(
                GateCheck(
                    name="placebo_dominance",
                    status="pass" if placebo_ok else "fail",
                    message=(
                        f"candidate Sharpe {candidate_sharpe:.4f} exceeds "
                        f"the {config.placebo_percentile:.0%} percentile "
                        f"placebo Sharpe {percentile:.4f}"
                    ),
                    evidence={
                        "percentile": percentile,
                        "placebo_samples": len(placebo_sharpes),
                        "candidate_sharpe": candidate_sharpe,
                    },
                )
            )

        # -- verdict ----------------------------------------------------------------------
        metrics.update(candidate_metrics)
        metrics.update(
            {
                "max_drawdown": max_drawdown,
                "annualized_turnover": annual_turnover,
                "cost_drag": cost_drag,
                "cost_share": cost_share,
            }
        )
        failed = [check for check in checks if check.status == "fail"]
        warned = [check for check in checks if check.status == "warn"]
        if failed:
            verdict = GateVerdict.FAIL.value
        elif warned:
            verdict = GateVerdict.FRAGILE.value
        else:
            verdict = GateVerdict.PASS.value
        score = (
            sum(
                {"pass": 1.0, "warn": 0.5, "fail": 0.0}[check.status]
                for check in checks
            )
            / len(checks)
            * 100.0
        )
        return GateDecision(
            verdict=verdict,
            score=score,
            checks=tuple(checks),
            metrics=metrics,
            benchmarks=benchmark_metrics,
            confidence_intervals={
                name: interval.to_dict() for name, interval in intervals.items()
            },
            reproducibility=self._reproducibility(
                result,
                strategy_version=strategy_version,
                factor_versions=dict(factor_versions or {}),
                universe=universe,
                cost_model_name=cost_model_name,
                rebalance_frequency=rebalance_frequency,
                validation_method=validation_method,
            ),
            generated_at=generated_at or datetime.now(UTC),
        )

    def _reproducibility(
        self,
        result: BacktestResult,
        *,
        strategy_version: str,
        factor_versions: Mapping[str, str],
        universe: str,
        cost_model_name: str,
        rebalance_frequency: str,
        validation_method: str,
    ) -> dict[str, Any]:
        return {
            "strategy": result.strategy_name,
            "strategy_version": strategy_version,
            "factor_versions": dict(factor_versions),
            "universe": universe,
            "rebalance_frequency": rebalance_frequency,
            "cost_model": cost_model_name,
            "validation_method": validation_method,
            "random_seed": self.random_seed,
            "git_commit": self.git_commit,
            "dataset_fingerprint": self.dataset_fingerprint,
            "engine_metadata": dict(result.metadata),
        }


def _mapping_consistency(validation: Mapping[str, Any]) -> dict[str, Any]:
    """Read fold consistency from a plain mapping (e.g. report payload)."""
    required = {"positive_fold_fraction", "folds"}
    missing = required - set(validation)
    if missing:
        raise ResearchInputError(
            "validation mapping is missing consistency fields: "
            + ", ".join(sorted(missing))
        )
    return {
        "positive_fold_fraction": float(validation["positive_fold_fraction"]),
        "folds": int(validation["folds"]),
        "worst_fold_sharpe": float(validation.get("worst_fold_sharpe", 0.0)),
        "aggregate_deflated_sharpe_probability": float(
            validation.get("aggregate_deflated_sharpe_probability", 0.0)
        ),
    }
