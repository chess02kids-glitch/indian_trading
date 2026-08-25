"""Reusable temporal validation splits and statistical backtest corrections.

This module implements the statistical validation layer of the research
platform:

* **Deflated Sharpe Ratio** (Bailey & López de Prado, 2014): skew/kurtosis
  adjusted, multiple-testing corrected probability that the observed Sharpe
  is not the lucky maximum of the trials that were attempted.
* **Purged walk-forward validation**: rolling/expanding train/test windows
  with label-purge windows and embargo gaps, guaranteeing zero look-ahead.
* **Combinatorial purged cross-validation** (CPCV): deterministic
  ``C(n, k)`` train/test paths with reproducible, parallel-safe folds.
* **Bootstrap confidence intervals** for Sharpe, CAGR, max drawdown,
  volatility, and turnover with configurable iterations and seeds.

Every function is pure, deterministic, and clock-free: identical inputs
always produce identical outputs.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Any, Sequence

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

__all__ = [
    "BOOTSTRAP_METRICS",
    "BootstrapConfidenceInterval",
    "CrossValidationResult",
    "DeflatedSharpeResult",
    "HoldoutProtocolResult",
    "HoldoutSplit",
    "ValidationWindow",
    "WalkForwardResult",
    "bootstrap_metric_intervals",
    "bootstrap_sharpe_confidence_interval",
    "combinatorial_purged_cv",
    "deflated_sharpe_from_returns",
    "deflated_sharpe_ratio",
    "expected_maximum_sharpe",
    "holdout_split",
    "run_combinatorial_purged_cv",
    "run_holdout_protocol",
    "run_walk_forward",
    "validation_consistency",
    "walk_forward_splits",
]

#: Euler–Mascheroni constant used by the expected-maximum approximation.
_EULER_GAMMA = 0.5772156649015329

#: Bootstrap metrics supported by :func:`bootstrap_metric_intervals`.
BOOTSTRAP_METRICS = ("sharpe", "cagr", "max_drawdown", "volatility", "turnover")


@dataclass(frozen=True, slots=True)
class ValidationWindow:
    """One train/test window with explicit purge and embargo metadata.

    Parameters
    ----------
    purge:
        Number of trailing training observations removed because their label
        look-back overlaps the first test observation (label leakage). The
        retained training series is ``train_index[:-purge]``.
    embargo:
        Number of observations between the end of the (purged) training
        window and the start of the test window.
    """

    fold: int
    train_index: pd.DatetimeIndex
    test_index: pd.DatetimeIndex
    purge: int = 0
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
            "purge": self.purge,
            "embargo": self.embargo,
        }


def _validate_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if not isinstance(index, pd.DatetimeIndex) or index.empty:
        raise ResearchInputError("validation index must be a non-empty DatetimeIndex")
    if not index.is_unique or not index.is_monotonic_increasing:
        raise ResearchInputError("validation index must be sorted and unique")
    return index


def _validate_gap_parameters(purge: int, embargo: int) -> None:
    for value, name in ((purge, "purge"), (embargo, "embargo")):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ResearchInputError(f"{name} must be a non-negative integer")


def walk_forward_splits(
    index: pd.DatetimeIndex,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
    *,
    expanding: bool = False,
    purge: int = 0,
    embargo: int = 0,
) -> tuple[ValidationWindow, ...]:
    """Create rolling or expanding train/test windows without temporal leakage.

    The test window always starts strictly after the *purged* training
    window plus the embargo gap, so no observation whose label look-back
    overlaps the test period can influence training (zero look-ahead).

    Parameters
    ----------
    index:
        Sorted unique DatetimeIndex used for both training and testing.
    train_size:
        Number of observations in each raw training window.
    test_size:
        Number of observations in each test window.
    step_size:
        Step between window starts; defaults to ``test_size`` (non-overlapping
        tests).
    expanding:
        When True the training window starts at the beginning of the index
        (expanding), otherwise it is rolling.
    purge:
        Number of trailing training observations to remove because their
        label look-back overlaps the first test observation.
    embargo:
        Observations between the purged training end and the test start.
    """
    index = _validate_index(index)
    if train_size < 1 or test_size < 1:
        raise ResearchInputError("train_size and test_size must be positive")
    if step_size is not None and (step_size < 1 or step_size > len(index)):
        raise ResearchInputError("step_size must be positive and no greater than n")
    if train_size + test_size > len(index):
        raise ResearchInputError("index does not contain a complete train/test window")
    _validate_gap_parameters(purge, embargo)
    if purge >= train_size:
        raise ResearchInputError("purge must be smaller than train_size")
    step = test_size if step_size is None else step_size
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
        retained_end = train_end - purge
        windows.append(
            ValidationWindow(
                fold=fold,
                train_index=index[train_start:retained_end],
                test_index=index[test_start:test_end],
                purge=purge,
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
    purge: int = 0,
    embargo: int = 0,
) -> tuple[ValidationWindow, ...]:
    """Create combinatorial purged cross-validation (CPCV) splits.

    The index is divided into ``n_groups`` contiguous, equal-sized groups.
    Every ``C(n_groups, n_test_groups)`` combination of test groups is
    evaluated. Training observations within ``purge`` rows of any test group
    are removed (their labels overlap the test labels); ``embargo`` widens
    the exclusion on both sides as an extra safety gap. This is the
    point-in-time CPCV design of López de Prado (2018, ch. 12).

    The split construction is deterministic (no random number generator is
    used), reproducible across processes, and safe to run in parallel because
    each window is fully independent of any shared mutable state.
    """
    index = _validate_index(index)
    if n_groups < 2 or not 1 <= n_test_groups < n_groups:
        raise ResearchInputError(
            "n_groups must exceed n_test_groups, both at least one"
        )
    if n_groups > len(index):
        raise ResearchInputError("n_groups cannot exceed observations")
    _validate_gap_parameters(purge, embargo)
    exclusion_radius = purge + embargo
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
        excluded = {
            position
            for test_position in test_positions
            for position in range(
                max(0, int(test_position) - exclusion_radius),
                min(len(index), int(test_position) + exclusion_radius + 1),
            )
        }
        train_positions = [
            position
            for position in range(len(index))
            if position not in test_set and position not in excluded
        ]
        if not train_positions:
            raise ResearchInputError("purge/embargo removed every training observation")
        windows.append(
            ValidationWindow(
                fold=fold,
                train_index=index[train_positions],
                test_index=index[test_positions],
                purge=purge,
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


def expected_maximum_sharpe(
    trials: int,
    *,
    standard_error: float = 1.0,
) -> float:
    """Expected maximum Sharpe of ``trials`` independent standard trials.

    Implements the Bailey & López de Prado (2014) approximation:

    ``E[max SR] ~= (1 - gamma) Z^-1(1 - 1/N) + gamma Z^-1(1 - 1/(N e))``

    scaled by the standard error of the Sharpe estimator when supplied. A
    single trial has an expected maximum of zero under the null.
    """
    if trials < 1:
        raise ResearchInputError("trials must be positive")
    if not math.isfinite(standard_error) or standard_error <= 0:
        raise ResearchInputError("standard_error must be finite and positive")
    normal = NormalDist()
    if trials == 1:
        expected = 0.0
    else:
        expected = (1 - _EULER_GAMMA) * normal.inv_cdf(
            1 - 1 / trials
        ) + _EULER_GAMMA * normal.inv_cdf(1 - 1 / (trials * math.e))
    return float(expected * standard_error)


def deflated_sharpe_ratio(
    observed_sharpe: float,
    trials: int,
    observations: int,
    *,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    expected_max_sharpe: float | None = None,
) -> DeflatedSharpeResult:
    """Estimate the probability a Sharpe survives multiple-testing correction.

    Uses the Bailey & López de Prado (2014) adjustment:

    * the Sharpe estimator is corrected for non-normal returns through its
      asymptotic variance

      ``V[SR] = (1 - skew * SR + (kurtosis - 1) / 4 * SR^2) / (T - 1)``

    * ``expected_max_sharpe`` is the expected maximum of ``trials``
      independent attempts under the null (optionally supplied by the caller
      for non-independent trial families).

    Returns ``Phi((SR - E[max SR]) / sqrt(V[SR]))`` — the probability that
    the observed Sharpe exceeds the multiple-testing hurdle. A probability
    below the configured acceptance threshold means the result is
    indistinguishable from the best of ``trials`` random strategies.
    """
    if trials < 1 or observations < 2:
        raise ResearchInputError(
            "trials must be positive and observations at least two"
        )
    values = (observed_sharpe, skewness, kurtosis)
    if not all(math.isfinite(value) for value in values):
        raise ResearchInputError("Sharpe distribution inputs must be finite")
    variance = (
        1 - skewness * observed_sharpe + (kurtosis - 1) / 4 * observed_sharpe**2
    ) / (observations - 1)
    if variance <= 0 or not math.isfinite(variance):
        raise ResearchInputError("Sharpe distribution variance is not positive")
    standard_error = math.sqrt(variance)
    if expected_max_sharpe is None:
        expected_max = expected_maximum_sharpe(trials, standard_error=standard_error)
    else:
        if not math.isfinite(expected_max_sharpe):
            raise ResearchInputError("expected_max_sharpe must be finite")
        expected_max = float(expected_max_sharpe)
    probability = NormalDist().cdf((observed_sharpe - expected_max) / standard_error)
    return DeflatedSharpeResult(
        observed_sharpe=float(observed_sharpe),
        expected_max_sharpe=float(expected_max),
        standard_error=float(standard_error),
        probability=float(probability),
        trials=trials,
        observations=observations,
    )


def _sample_moments(values: np.ndarray) -> tuple[float, float]:
    """Compute bias-adjusted third/fourth central moments of returns.

    The deflated Sharpe implementation follows the paper's Appendix: the raw
    moments are computed on the sample and bias-corrected with the standard
    sample factors so that a normal sample returns (skew, kurtosis) ~ (0, 3).
    """
    centered = values - values.mean()
    second = float(np.mean(centered**2))
    standard_deviation = math.sqrt(second) if second > 0 else 0.0
    if standard_deviation == 0:
        return 0.0, 3.0
    n = len(values)
    third = float(np.mean(centered**3))
    fourth = float(np.mean(centered**4))
    # Bias adjustment factors (standard sample moment estimators).
    skewness = (
        math.sqrt(n * (n - 1)) / (n - 2) * third / standard_deviation**3
        if n > 2
        else 0.0
    )
    kurtosis = (
        (n - 1) / ((n - 2) * (n - 3)) * ((n + 1) * fourth / standard_deviation**4 - 3)
        + 3
        if n > 3
        else 3.0
    )
    if not math.isfinite(skewness):
        skewness = 0.0
    if not math.isfinite(kurtosis):
        kurtosis = 3.0
    return float(skewness), float(kurtosis)


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
    array = values.to_numpy(dtype=float)
    if not np.isfinite(array).all():
        raise ResearchInputError("returns must be finite")
    standard_deviation = float(values.std(ddof=1))
    sharpe = (
        float(values.mean() / standard_deviation * math.sqrt(periods_per_year))
        if standard_deviation > 0
        else 0.0
    )
    if not math.isfinite(sharpe):
        sharpe = 0.0
    skewness, kurtosis = _sample_moments(array)
    return deflated_sharpe_ratio(
        sharpe,
        trials,
        len(values),
        skewness=skewness,
        kurtosis=kurtosis,
    )


@dataclass(frozen=True, slots=True)
class BootstrapConfidenceInterval:
    """Bootstrap estimate and percentile confidence interval for one metric."""

    metric: str
    estimate: float
    lower: float
    upper: float
    confidence: float
    samples: int
    seed: int
    block_length: int | None = None

    def to_dict(self) -> dict[str, float | int | str | None]:
        """Return a report-ready confidence interval mapping."""
        return {
            "metric": self.metric,
            "estimate": self.estimate,
            "lower": self.lower,
            "upper": self.upper,
            "confidence": self.confidence,
            "samples": self.samples,
            "seed": self.seed,
            "block_length": self.block_length,
        }


def _bootstrap_draws(
    length: int,
    samples: int,
    seed: int,
    block_length: int | None,
) -> np.ndarray:
    """Draw deterministic resample indices (iid or circular block)."""
    generator = np.random.default_rng(seed)
    if block_length is None or block_length < 1:
        return generator.integers(0, length, size=(samples, length))
    if block_length > length:
        raise ResearchInputError("block_length cannot exceed observations")
    n_blocks = int(math.ceil(length / block_length))
    starts = generator.integers(0, length, size=(samples, n_blocks))
    offsets = np.arange(block_length)
    drawn = (starts[:, :, None] + offsets[None, None, :]) % length
    return drawn.reshape(samples, -1)[:, :length]


def _annualized_return(values: np.ndarray, periods_per_year: int) -> float:
    growth = np.prod(1.0 + values)
    if growth <= 0 or not math.isfinite(growth):
        return 0.0
    return float(growth ** (periods_per_year / len(values)) - 1.0)


def _annualized_volatility(values: np.ndarray, periods_per_year: int) -> float:
    deviation = float(values.std(ddof=1))
    return deviation * math.sqrt(periods_per_year)


def _max_drawdown(values: np.ndarray) -> float:
    equity = np.cumprod(1.0 + values)
    peaks = np.maximum.accumulate(equity)
    ratio = np.divide(
        equity,
        peaks,
        out=np.ones_like(equity),
        where=peaks > 0,
    )
    return float(np.min(ratio) - 1.0)


def _metric_from_sample(
    values: np.ndarray,
    period_returns: np.ndarray,
    turnover_values: np.ndarray | None,
    metric: str,
    periods_per_year: int,
    years: float,
) -> float:
    """Compute one bootstrap statistic from a 1-D resampled return path."""
    if metric == "sharpe":
        deviation = float(period_returns.std(ddof=1))
        return (
            float(period_returns.mean() / deviation * math.sqrt(periods_per_year))
            if deviation > 0
            else 0.0
        )
    if metric == "cagr":
        return _annualized_return(period_returns, periods_per_year)
    if metric == "max_drawdown":
        return _max_drawdown(period_returns)
    if metric == "volatility":
        return _annualized_volatility(period_returns, periods_per_year)
    if metric == "turnover":
        if turnover_values is None:
            raise ResearchInputError("turnover series is required for turnover CI")
        return float(turnover_values[values].sum() / years)
    raise ResearchInputError(f"unsupported bootstrap metric: {metric}")


def bootstrap_metric_intervals(
    returns: pd.Series,
    *,
    turnover: pd.Series | None = None,
    metrics: Sequence[str] | None = None,
    samples: int = 2_000,
    confidence: float = 0.95,
    seed: int = 42,
    periods_per_year: int = 252,
    initial_value: float = 1.0,
    block_length: int | None = None,
) -> dict[str, BootstrapConfidenceInterval]:
    """Estimate deterministic percentile bootstrap CIs for validation metrics.

    Resamples (with replacement) the return path ``samples`` times — iid by
    default, or circular-block when ``block_length`` is supplied — and
    computes Sharpe, CAGR, max drawdown, volatility, and annualized turnover
    on every path. The same resample matrix drives every metric so the
    intervals are internally consistent.

    When ``metrics`` is None all metrics are computed, except turnover which
    requires a ``turnover`` series. Returns a mapping
    ``metric -> BootstrapConfidenceInterval``; the bootstrap is
    deterministic for a fixed ``seed``.
    """
    if samples < 100 or not 0 < confidence < 1 or periods_per_year < 1:
        raise ResearchInputError(
            "samples, confidence, and periods_per_year are invalid"
        )
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ResearchInputError("seed must be an integer")
    if initial_value <= 0:
        raise ResearchInputError("initial_value must be positive")
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
    if turnover is not None:
        if not isinstance(turnover, pd.Series):
            raise ResearchInputError("turnover must be a pandas Series")
        if not turnover.index.equals(returns.index):
            raise ResearchInputError("turnover must align with returns")
        numeric_turnover = pd.to_numeric(turnover, errors="coerce")
        if (
            numeric_turnover.isna().any()
            or (numeric_turnover < 0).any()
            or not np.isfinite(numeric_turnover.to_numpy()).all()
        ):
            raise ResearchInputError("turnover must be finite and non-negative")
        turnover_values = numeric_turnover.to_numpy(dtype=float)
    else:
        turnover_values = None
    if metrics is None:
        metrics = (
            BOOTSTRAP_METRICS if turnover_values is not None else BOOTSTRAP_METRICS[:-1]
        )
    if "turnover" in metrics and turnover_values is None:
        raise ResearchInputError("turnover series is required for turnover CI")
    unknown = set(metrics) - set(BOOTSTRAP_METRICS)
    if unknown:
        raise ResearchInputError(f"unsupported bootstrap metrics: {sorted(unknown)}")
    if not metrics:
        raise ResearchInputError("at least one metric is required")

    drawn = _bootstrap_draws(len(values), samples, seed, block_length)
    years = len(values) / periods_per_year
    alpha = (1 - confidence) / 2
    output: dict[str, BootstrapConfidenceInterval] = {}
    # Point estimates (computed on the observed path, not resampled).
    point_estimates = {
        "sharpe": (
            float(values.mean() / values.std(ddof=1) * math.sqrt(periods_per_year))
            if values.std(ddof=1) > 0
            else 0.0
        ),
        "cagr": _annualized_return(values, periods_per_year),
        "max_drawdown": _max_drawdown(values),
        "volatility": _annualized_volatility(values, periods_per_year),
        "turnover": (
            float(turnover_values.sum() / years) if turnover_values is not None else 0.0
        ),
    }
    for metric in metrics:
        sampled_returns = values[drawn]
        statistics = np.empty(samples, dtype=float)
        for index in range(samples):
            statistics[index] = _metric_from_sample(
                drawn[index],
                sampled_returns[index],
                turnover_values,
                metric,
                periods_per_year,
                years,
            )
        lower, upper = np.quantile(statistics, [alpha, 1 - alpha])
        output[metric] = BootstrapConfidenceInterval(
            metric=metric,
            estimate=point_estimates[metric],
            lower=float(lower),
            upper=float(upper),
            confidence=confidence,
            samples=samples,
            seed=seed,
            block_length=block_length,
        )
    return output


def bootstrap_sharpe_confidence_interval(
    returns: pd.Series,
    *,
    samples: int = 2_000,
    confidence: float = 0.95,
    seed: int = 42,
    periods_per_year: int = 252,
) -> BootstrapConfidenceInterval:
    """Estimate a deterministic percentile bootstrap interval for Sharpe.

    Thin wrapper over :func:`bootstrap_metric_intervals` retained for
    backward compatibility.
    """
    intervals = bootstrap_metric_intervals(
        returns,
        metrics=("sharpe",),
        samples=samples,
        confidence=confidence,
        seed=seed,
        periods_per_year=periods_per_year,
    )
    return intervals["sharpe"]


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Fold backtests and aggregate metrics from walk-forward evaluation."""

    windows: tuple[ValidationWindow, ...]
    fold_results: tuple[BacktestResult, ...]
    aggregate_metrics: PerformanceMetrics
    method: str = "walk_forward"

    def to_dict(self) -> dict[str, Any]:
        """Return fold and aggregate validation output."""
        return {
            "method": self.method,
            "windows": [window.to_dict() for window in self.windows],
            "fold_metrics": [result.metrics.to_dict() for result in self.fold_results],
            "aggregate_metrics": self.aggregate_metrics.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CrossValidationResult:
    """Fold backtests from combinatorial purged cross-validation."""

    windows: tuple[ValidationWindow, ...]
    fold_results: tuple[BacktestResult, ...]
    aggregate_metrics: PerformanceMetrics
    method: str = "cpcv"

    def to_dict(self) -> dict[str, Any]:
        """Return fold and aggregate validation output."""
        return {
            "method": self.method,
            "windows": [window.to_dict() for window in self.windows],
            "fold_metrics": [result.metrics.to_dict() for result in self.fold_results],
            "aggregate_metrics": self.aggregate_metrics.to_dict(),
        }


def _evaluate_windows(
    strategy: Strategy,
    data: MarketData,
    constructor: PortfolioConstructor,
    engine: VectorBTResearchEngine,
    windows: tuple[ValidationWindow, ...],
    method: str,
) -> tuple[tuple[BacktestResult, ...], PerformanceMetrics]:
    """Backtest each validation window on its test slice only.

    Signals are computed once from the full (point-in-time) panel and then
    sliced to the test window; because every factor and constructor is
    trailing-only, observations after ``t`` never influence the value at
    ``t``. Fold results never include training observations.
    """
    if not windows:
        raise ResearchInputError("no validation windows supplied")
    signals = strategy.generate_signals(data)
    weights = constructor.construct(signals, data)
    fold_results = tuple(
        engine.run(
            data.close.loc[window.test_index],
            weights.loc[window.test_index],
            strategy_name=f"{strategy.name}_{method}_fold_{window.fold}",
            universe_history=[],
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
    return fold_results, metrics


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
    purge: int = 0,
    embargo: int = 0,
) -> WalkForwardResult:
    """Evaluate deterministic strategy signals on non-overlapping test windows.

    ``purge`` removes training observations whose label look-back overlaps
    the test window; ``embargo`` inserts an additional gap between the
    purged training window and the test window.
    """
    windows = walk_forward_splits(
        data.close.index,
        train_size=train_size,
        test_size=test_size,
        step_size=step_size,
        expanding=expanding,
        purge=purge,
        embargo=embargo,
    )
    fold_results, metrics = _evaluate_windows(
        strategy, data, constructor, engine, windows, "walk_forward"
    )
    return WalkForwardResult(windows, fold_results, metrics)


def run_combinatorial_purged_cv(
    strategy: Strategy,
    data: MarketData,
    constructor: PortfolioConstructor,
    engine: VectorBTResearchEngine,
    n_groups: int,
    n_test_groups: int,
    *,
    purge: int = 0,
    embargo: int = 0,
) -> CrossValidationResult:
    """Evaluate all CPCV path combinations, reusing the trailing point-in-time
    signals without any look-ahead."""
    windows = combinatorial_purged_cv(
        data.close.index,
        n_groups=n_groups,
        n_test_groups=n_test_groups,
        purge=purge,
        embargo=embargo,
    )
    fold_results, metrics = _evaluate_windows(
        strategy, data, constructor, engine, windows, "cpcv"
    )
    return CrossValidationResult(windows, fold_results, metrics)


def validation_consistency(
    result: WalkForwardResult | CrossValidationResult,
    *,
    min_positive_sharpe: float = 0.0,
) -> dict[str, Any]:
    """Summarize fold-level consistency of a cross-validation run.

    Returns the fraction of folds with Sharpe above ``min_positive_sharpe``,
    the best/worst fold Sharpe, the standard deviation of fold Sharpes, and
    the aggregate deflated Sharpe over all fold returns. Deterministic.
    """
    fold_metrics = [fold.metrics.to_dict() for fold in result.fold_results]
    if not fold_metrics:
        raise ResearchInputError("validation result has no folds")
    sharpes = np.array([float(metrics["sharpe"]) for metrics in fold_metrics])
    positive = float((sharpes > min_positive_sharpe).mean())
    aggregate_returns = pd.concat(
        [fold.returns for fold in result.fold_results]
    ).sort_index()
    aggregate_returns = aggregate_returns[
        ~aggregate_returns.index.duplicated(keep="first")
    ]
    dsr = deflated_sharpe_from_returns(aggregate_returns, trials=len(fold_metrics) + 1)
    return {
        "folds": len(fold_metrics),
        "positive_fold_fraction": positive,
        "best_fold_sharpe": float(sharpes.max()),
        "worst_fold_sharpe": float(sharpes.min()),
        # A single fold has no dispersion to measure.
        "fold_sharpe_std": float(sharpes.std(ddof=1)) if len(sharpes) > 1 else 0.0,
        "aggregate_deflated_sharpe_probability": dsr.probability,
    }


@dataclass(frozen=True, slots=True)
class HoldoutSplit:
    """Chronological partition of a research timeline.

    The development prefix is the only region where walk-forward and
    combinatorial-purged cross-validation may train or test. The trailing
    holdout is *locked*: it is never used for signal evaluation during
    development and receives exactly one candidate evaluation.
    """

    dev_index: pd.DatetimeIndex
    holdout_index: pd.DatetimeIndex
    holdout_size: int

    def __post_init__(self) -> None:
        dev = _validate_index(self.dev_index)
        holdout = _validate_index(self.holdout_index)
        if (
            not isinstance(self.holdout_size, int)
            or isinstance(self.holdout_size, bool)
            or self.holdout_size < 2
        ):
            raise ResearchInputError("holdout_size must be an integer of at least 2")
        if len(holdout) != self.holdout_size:
            raise ResearchInputError("holdout_index length must equal holdout_size")
        if len(dev) < 2:
            raise ResearchInputError("development index must have at least 2 rows")
        if dev[-1] >= holdout[0]:
            raise ResearchInputError(
                "development observations must end strictly before the holdout"
            )
        object.__setattr__(self, "dev_index", dev)
        object.__setattr__(self, "holdout_index", holdout)

    @property
    def dev_start(self) -> pd.Timestamp:
        """First development observation."""
        return self.dev_index[0]

    @property
    def dev_end(self) -> pd.Timestamp:
        """Last development observation (never traded in the holdout run)."""
        return self.dev_index[-1]

    @property
    def holdout_start(self) -> pd.Timestamp:
        """First locked holdout observation."""
        return self.holdout_index[0]

    @property
    def holdout_end(self) -> pd.Timestamp:
        """Last locked holdout observation."""
        return self.holdout_index[-1]

    def to_dict(self) -> dict[str, Any]:
        """Return the explicit, reproducible split boundaries."""
        return {
            "dev_start": self.dev_start.isoformat(),
            "dev_end": self.dev_end.isoformat(),
            "dev_size": len(self.dev_index),
            "holdout_start": self.holdout_start.isoformat(),
            "holdout_end": self.holdout_end.isoformat(),
            "holdout_size": len(self.holdout_index),
        }


def holdout_split(index: pd.DatetimeIndex, holdout_size: int) -> HoldoutSplit:
    """Partition a sorted index into a development prefix and locked holdout.

    The holdout is the trailing ``holdout_size`` observations; everything
    before it is development data. The boundaries are a pure function of the
    index, so identical inputs always produce identical splits.
    """
    validated = _validate_index(index)
    if (
        not isinstance(holdout_size, int)
        or isinstance(holdout_size, bool)
        or holdout_size < 2
    ):
        raise ResearchInputError("holdout_size must be an integer of at least 2")
    if len(validated) - holdout_size < 2:
        raise ResearchInputError(
            "index must retain at least two development observations"
        )
    holdout = validated[len(validated) - holdout_size :]
    dev = validated[: len(validated) - holdout_size]
    return HoldoutSplit(dev_index=dev, holdout_index=holdout, holdout_size=holdout_size)


@dataclass(frozen=True, slots=True)
class HoldoutProtocolResult:
    """Full TRAIN -> VALIDATION -> LOCKED HOLDOUT evaluation of one candidate.

    * ``walk_forward`` (and optional ``cpcv``) are computed on the
      development prefix only — their windows can never overlap the holdout.
    * ``holdout_result`` is the single, final evaluation of the candidate on
      the locked holdout slice. Its signals are point-in-time: each weight
      uses only information available at or before its own date.
    """

    split: HoldoutSplit
    walk_forward: WalkForwardResult
    cpcv: CrossValidationResult | None
    holdout_result: BacktestResult

    def to_dict(self) -> dict[str, Any]:
        """Return boundary + per-period metrics without wall-clock fields."""
        return {
            "split": self.split.to_dict(),
            "walk_forward": self.walk_forward.to_dict(),
            "cpcv": self.cpcv.to_dict() if self.cpcv is not None else None,
            "holdout": {
                "start": self.holdout_result.returns.index[0].isoformat(),
                "end": self.holdout_result.returns.index[-1].isoformat(),
                "metrics": self.holdout_result.metrics.to_dict(),
            },
        }


def _restrict_to_index(data: MarketData, index: pd.DatetimeIndex) -> MarketData:
    """Slice an aligned MarketData panel to a sub-index, preserving alignment."""

    def _slice(panel: pd.DataFrame | None) -> pd.DataFrame | None:
        return panel.loc[index] if panel is not None else None

    return MarketData(
        close=_slice(data.close),
        high=_slice(data.high),
        low=_slice(data.low),
        volume=_slice(data.volume),
    )


def _assert_windows_disjoint_from_holdout(
    windows: Sequence[ValidationWindow], split: HoldoutSplit
) -> None:
    """Explicit look-ahead guard: no validation window may touch the holdout."""
    for window in windows:
        if window.train_index[-1] >= split.holdout_start:
            raise ResearchInputError(
                f"walk-forward fold {window.fold} training window overlaps the "
                "locked holdout; the holdout must remain untouched during "
                "strategy development"
            )
        if window.test_index[-1] >= split.holdout_start:
            raise ResearchInputError(
                f"walk-forward fold {window.fold} test window overlaps the "
                "locked holdout; the holdout must remain untouched during "
                "strategy development"
            )


def run_holdout_protocol(
    strategy: Strategy,
    data: MarketData,
    constructor: PortfolioConstructor,
    engine: VectorBTResearchEngine,
    holdout_size: int,
    *,
    train_size: int,
    test_size: int,
    step_size: int | None = None,
    expanding: bool = False,
    purge: int = 0,
    embargo: int = 0,
    cpcv_n_groups: int | None = None,
    cpcv_n_test_groups: int | None = None,
    universe_history: Sequence[Any] | None = None,
    holdout_strategy_name: str | None = None,
) -> HoldoutProtocolResult:
    """Run the chronological TRAIN -> VALIDATION -> LOCKED HOLDOUT protocol.

    1. The timeline is split into a development prefix and a trailing locked
       holdout (``holdout_split``).
    2. Walk-forward (and optionally CPCV) validation runs on the development
       prefix *only*, with an explicit guard that no window touches the
       holdout.
    3. The candidate is evaluated exactly once on the locked holdout slice.
       Signals are computed from the full point-in-time panel so trailing
       look-backs that start before the holdout still work, but no weight at
       a holdout date ever uses information after that date.

    Deterministic: identical inputs produce identical splits, fold results,
    and holdout results.
    """
    split = holdout_split(data.close.index, holdout_size)
    dev_data = _restrict_to_index(data, split.dev_index)

    walk_forward = run_walk_forward(
        strategy,
        dev_data,
        constructor,
        engine,
        train_size=train_size,
        test_size=test_size,
        step_size=step_size,
        expanding=expanding,
        purge=purge,
        embargo=embargo,
    )
    cpcv: CrossValidationResult | None = None
    if cpcv_n_groups is not None and cpcv_n_test_groups is not None:
        cpcv = run_combinatorial_purged_cv(
            strategy,
            dev_data,
            constructor,
            engine,
            n_groups=cpcv_n_groups,
            n_test_groups=cpcv_n_test_groups,
            purge=purge,
            embargo=embargo,
        )

    _assert_windows_disjoint_from_holdout(walk_forward.windows, split)
    if cpcv is not None:
        _assert_windows_disjoint_from_holdout(cpcv.windows, split)

    signals = strategy.generate_signals(data)
    weights = constructor.construct(signals, data)
    if not weights.index.equals(data.close.index):
        raise ResearchInputError(
            "constructor weights must align exactly with the research timeline"
        )
    holdout_result = engine.run(
        data.close.loc[split.holdout_index],
        weights.loc[split.holdout_index],
        strategy_name=holdout_strategy_name or f"{strategy.name}_holdout",
        universe_history=list(universe_history or ()),
    )
    if not holdout_result.returns.index.equals(split.holdout_index):
        raise ResearchInputError("holdout evaluation must cover the holdout exactly")
    return HoldoutProtocolResult(
        split=split,
        walk_forward=walk_forward,
        cpcv=cpcv,
        holdout_result=holdout_result,
    )
