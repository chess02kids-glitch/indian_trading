"""Reusable deterministic backtesting, benchmark, metric, and validation utilities."""

from .benchmarks import (
    BENCHMARK_NAMES,
    benchmark_suite,
    buy_and_hold_weights,
    compare_results,
    equal_weight_weights,
    inverse_volatility_weights,
    persistence_weights,
    random_weights,
)
from .engine import BacktestConfig, BacktestResult, VectorBTResearchEngine
from .metrics import (
    PerformanceMetrics,
    compute_performance_metrics,
    drawdown,
    equity_curve,
    rolling_sharpe,
)
from .validation import (
    BootstrapConfidenceInterval,
    DeflatedSharpeResult,
    ValidationWindow,
    WalkForwardResult,
    bootstrap_sharpe_confidence_interval,
    combinatorial_purged_cv,
    deflated_sharpe_from_returns,
    deflated_sharpe_ratio,
    run_walk_forward,
    walk_forward_splits,
)

__all__ = [
    "BENCHMARK_NAMES",
    "BacktestConfig",
    "BacktestResult",
    "BootstrapConfidenceInterval",
    "DeflatedSharpeResult",
    "PerformanceMetrics",
    "ValidationWindow",
    "VectorBTResearchEngine",
    "WalkForwardResult",
    "benchmark_suite",
    "bootstrap_sharpe_confidence_interval",
    "buy_and_hold_weights",
    "combinatorial_purged_cv",
    "compare_results",
    "compute_performance_metrics",
    "deflated_sharpe_from_returns",
    "deflated_sharpe_ratio",
    "drawdown",
    "equal_weight_weights",
    "equity_curve",
    "inverse_volatility_weights",
    "persistence_weights",
    "random_weights",
    "rolling_sharpe",
    "run_walk_forward",
    "walk_forward_splits",
]
