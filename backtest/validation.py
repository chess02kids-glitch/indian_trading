"""Reusable temporal validation splits and statistical backtest corrections."""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any

import numpy as np
import pandas as pd

from research.contracts import (
    MarketData,
    PortfolioConstructor,
    ResearchInputError,
    Strategy,
)

from .engine import BacktestResult, VectorBTResearchEngine
from .metrics import PerformanceMetrics, compute_performance_metrics


@dataclass(frozen=True, slots=True)
class ValidationWindow:
    """One train/test window with explicit temporal embargo metadata."""

    fold: int
    train_index: pd.DatetimeIndex
    test_index: pd.DatetimeIndex
    embargo: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable split description."""
        return {
            "fold": self.fold,
            "train_start": self.train_index[0].isoformat(),
            "train_end": self.train_index[-1].isoformat(),
            "test_start": self.test_index[0].isoformat(),
            "test_end": self.test_index[-1].isoformat(),
            "train_size": len(self.train_index),
            "test_size": len(self.test_index),
            "embargo": self.embargo,
        }


def _validate_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex) or index.empty:
        raise ResearchInputError("validation index must be a non-empty DatetimeIndex")
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ResearchInputError("validation index must be sorted and unique")
    return index


def walk_forward_splits(
    index: pd.DatetimeIndex,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
    *,
    expanding: bool = False,
    embargo: int = 0,
) -> tuple[ValidationWindow, ...]:
    """Create rolling or expanding train/test windows without temporal leakage."""
    index = _validate_index(index)
    if train_size < 1 or test_size < 1:
        raise ResearchInputError("train_size and test_size must be positive")
    step = test_size if step_size is None else step_size
    if step < 1 or embargo < 0:
        raise ResearchInputError("step_size must be positive and embargo non-negative")
    windows: list[ValidationWindow] = []
    start = 0
    fold = 0
    while True:
        train_start = 0 if expanding else start
        train_end = start + train_size
        test_start = train_end + embargo
        test_end = test_start + test_size
        if test_end > len(index):
            break
        windows.append(
            ValidationWindow(
                fold=fold,
                train_index=index[train_start:train_end],
                test_index=index[test_start:test_end],
                embargo=embargo,
            )
        )
        fold += 1
        start += step
    if not windows:
        raise ResearchInputError("index does not contain a complete train/test window")
    return tuple(windows)


def combinatorial_purged_cv(
    index: pd.DatetimeIndex,
    n_groups: int,
    n_test_groups: int,
    *,
    embargo: int = 0,
) -> tuple[ValidationWindow, ...]:
    """Create combinatorial purged cross-validation splits.

    The index is divided into contiguous groups. Every combination of test
    groups is evaluated while observations within ``embargo`` rows of each
    test group are removed from training. This is a practical CPCV
    implementation for point-in-time daily labels.
    """
    index = _validate_index(index)
    if n_groups < 2 or not 1 <= n_test_groups < n_groups:
        raise ResearchInputError(
            "n_groups must exceed n_test_groups, both at least one"
        )
    if n_groups > len(index) or embargo < 0:
        raise ResearchInputError(
            "n_groups cannot exceed observations and embargo cannot be negative"
        )
    positions = np.array_split(np.arange(len(index)), n_groups)
    windows: list[ValidationWindow] = []
    for fold, test_group_numbers in enumerate(
        itertools.combinations(range(n_groups), n_test_groups)
    ):
        test_positions = np.concatenate(
            [positions[number] for number in test_group_numbers]
        )
        test_positions.sort()
        test_set = set(int(position) for position in test_positions)
        purged = {
            position
            for test_position in test_positions
            for position in range(
                max(0, int(test_position) - embargo),
                min(len(index), int(test_position) + embargo + 1),
            )
        }
        train_positions = [
            position
            for position in range(len(index))
            if position not in test_set and position not in purged
        ]
        if not train_positions:
            raise ResearchInputError("embargo purged every training observation")
        windows.append(
            ValidationWindow(
                fold=fold,
                train_index=index[train_positions],
                test_index=index[test_positions],
                embargo=embargo,
            )
        )
    return tuple(windows)


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    """Deflated Sharpe probability and intermediate multiple-testing values."""

    observed_sharpe: float
    expected_max_sharpe: float
    standard_error: float
    probability: float
    trials: int
    observations: int

    def to_dict(self) -> dict[str, float | int]:
        """Return a report-ready DSR mapping."""
        return {
            "observed_sharpe": self.observed_sharpe,
            "expected_max_sharpe": self.expected_max_sharpe,
            "standard_error": self.standard_error,
            "probability": self.probability,
            "trials": self.trials,
            "observations": self.observations,
        }


def deflated_sharpe_ratio(
    observed_sharpe: float,
    trials: int,
    observations: int,
    *,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> DeflatedSharpeResult:
    """Estimate the probability a Sharpe survives multiple-testing correction."""
    if trials < 1 or observations < 2:
        raise ResearchInputError(
            "trials must be positive and observations at least two"
        )
    values = (observed_sharpe, skewness, kurtosis)
    if not all(math.isfinite(value) for value in values):
        raise ResearchInputError("Sharpe distribution inputs must be finite")
    normal = NormalDist()
    if trials == 1:
        expected_max = 0.0
    else:
        gamma = 0.5772156649015329
        expected_max = (1 - gamma) * normal.inv_cdf(
            1 - 1 / trials
        ) + gamma * normal.inv_cdf(1 - 1 / (trials * math.e))
    variance = (
        1 - skewness * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe**2
    ) / (observations - 1)
    if variance <= 0 or not math.isfinite(variance):
        raise ResearchInputError("Sharpe distribution variance is not positive")
    standard_error = math.sqrt(variance)
    probability = normal.cdf((observed_sharpe - expected_max) / standard_error)
    return DeflatedSharpeResult(
        observed_sharpe=float(observed_sharpe),
        expected_max_sharpe=float(expected_max),
        standard_error=float(standard_error),
        probability=float(probability),
        trials=trials,
        observations=observations,
    )


def deflated_sharpe_from_returns(
    returns: pd.Series,
    trials: int,
    *,
    periods_per_year: int = 252,
) -> DeflatedSharpeResult:
    """Estimate DSR from returns using sample skewness and raw kurtosis."""
    if periods_per_year < 1:
        raise ResearchInputError("periods_per_year must be positive")
    if not isinstance(returns, pd.Series):
        raise ResearchInputError("returns must be a pandas Series")
    values = pd.to_numeric(returns, errors="coerce")
    if values.isna().any() or len(values) < 2:
        raise ResearchInputError(
            "returns must contain at least two numeric observations"
        )
    standard_deviation = float(values.std(ddof=1))
    sharpe = float(values.mean() / standard_deviation * math.sqrt(periods_per_year))
    if not math.isfinite(sharpe):
        sharpe = 0.0
    skewness = float(values.skew())
    kurtosis = float(values.kurtosis() + 3)
    if not math.isfinite(skewness):
        skewness = 0.0
    if not math.isfinite(kurtosis):
        kurtosis = 3.0
    return deflated_sharpe_ratio(
        sharpe,
        trials,
        len(values),
        skewness=skewness,
        kurtosis=kurtosis,
    )


@dataclass(frozen=True, slots=True)
class BootstrapConfidenceInterval:
    """Bootstrap estimate and percentile confidence interval for Sharpe."""

    estimate: float
    lower: float
    upper: float
    confidence: float
    samples: int
    seed: int

    def to_dict(self) -> dict[str, float | int]:
        """Return a report-ready confidence interval mapping."""
        return {
            "estimate": self.estimate,
            "lower": self.lower,
            "upper": self.upper,
            "confidence": self.confidence,
            "samples": self.samples,
            "seed": self.seed,
        }


def bootstrap_sharpe_confidence_interval(
    returns: pd.Series,
    *,
    samples: int = 2_000,
    confidence: float = 0.95,
    seed: int = 42,
    periods_per_year: int = 252,
) -> BootstrapConfidenceInterval:
    """Estimate a deterministic percentile bootstrap interval for annualized Sharpe."""
    if samples < 100 or not 0 < confidence < 1 or periods_per_year < 1:
        raise ResearchInputError(
            "samples, confidence, and periods_per_year are invalid"
        )
    if not isinstance(seed, int):
        raise ResearchInputError("seed must be an integer")
    if not isinstance(returns, pd.Series):
        raise ResearchInputError("returns must be a pandas Series")
    numeric_returns = pd.to_numeric(returns, errors="coerce")
    if numeric_returns.isna().any():
        raise ResearchInputError("returns must not contain missing values")
    values = numeric_returns.to_numpy(dtype=float)
    if len(values) < 2 or not np.isfinite(values).all():
        raise ResearchInputError(
            "returns must contain at least two finite observations"
        )
    standard_deviation = float(values.std(ddof=1))
    estimate = (
        float(values.mean() / standard_deviation * math.sqrt(periods_per_year))
        if standard_deviation > 0
        else 0.0
    )
    generator = np.random.default_rng(seed)
    sampled_indices = generator.integers(0, len(values), size=(samples, len(values)))
    sampled = values[sampled_indices]
    means = sampled.mean(axis=1)
    deviations = sampled.std(axis=1, ddof=1)
    sharpes = np.divide(
        means * math.sqrt(periods_per_year),
        deviations,
        out=np.zeros_like(means),
        where=deviations > 0,
    )
    alpha = (1 - confidence) / 2
    lower, upper = np.quantile(sharpes, [alpha, 1 - alpha])
    return BootstrapConfidenceInterval(
        estimate=estimate,
        lower=float(lower),
        upper=float(upper),
        confidence=confidence,
        samples=samples,
        seed=seed,
    )


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Fold backtests and aggregate metrics from walk-forward evaluation."""

    windows: tuple[ValidationWindow, ...]
    fold_results: tuple[BacktestResult, ...]
    aggregate_metrics: PerformanceMetrics

    def to_dict(self) -> dict[str, Any]:
        """Return fold and aggregate validation output."""
        return {
            "windows": [window.to_dict() for window in self.windows],
            "fold_metrics": [result.metrics.to_dict() for result in self.fold_results],
            "aggregate_metrics": self.aggregate_metrics.to_dict(),
        }


def run_walk_forward(
    strategy: Strategy,
    data: MarketData,
    constructor: PortfolioConstructor,
    engine: VectorBTResearchEngine,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
    *,
    expanding: bool = False,
    embargo: int = 0,
) -> WalkForwardResult:
    """Evaluate deterministic strategy signals on non-overlapping test windows."""
    windows = walk_forward_splits(
        data.close.index,
        train_size=train_size,
        test_size=test_size,
        step_size=step_size,
        expanding=expanding,
        embargo=embargo,
    )
    signals = strategy.generate_signals(data)
    weights = constructor.construct(signals, data)
    fold_results = tuple(
        engine.run(
            data.close.loc[window.test_index],
            weights.loc[window.test_index],
            strategy_name=f"{strategy.name}_fold_{window.fold}",
        )
        for window in windows
    )
    aggregate_returns = pd.concat(
        [result.returns for result in fold_results]
    ).sort_index()
    aggregate_returns = aggregate_returns[
        ~aggregate_returns.index.duplicated(keep="first")
    ]
    aggregate_turnover = pd.concat(
        [result.trades["turnover"] for result in fold_results]
    ).sort_index()
    aggregate_turnover = aggregate_turnover[
        ~aggregate_turnover.index.duplicated(keep="first")
    ]
    metrics = compute_performance_metrics(
        aggregate_returns,
        aggregate_turnover,
        periods_per_year=engine.config.periods_per_year,
        initial_value=engine.config.initial_cash,
    )
    return WalkForwardResult(windows, fold_results, metrics)
