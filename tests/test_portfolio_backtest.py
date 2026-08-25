"""Tests for research portfolio construction, backtesting, and benchmarks."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.benchmarks import (
    BENCHMARK_NAMES,
    benchmark_suite,
    buy_and_hold_weights,
    compare_results,
    equal_weight_weights,
    inverse_volatility_weights,
    persistence_weights,
    random_weights,
)
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.metrics import (
    compute_performance_metrics,
    drawdown,
    equity_curve,
    rolling_sharpe,
)
from portfolio.construction import (
    AllocationConstraints,
    AllocationError,
    EqualWeightConstructor,
    InverseVolatilityConstructor,
    apply_constraints,
    equal_weight,
    inverse_volatility,
    risk_contributions,
    risk_parity_weights,
)
from research.contracts import CostModel, MarketData, ResearchInputError, Signal


def _prices(periods: int = 180) -> pd.DataFrame:
    """Build deterministic positive price panels."""
    index = pd.date_range("2023-01-02", periods=periods, freq="B")
    return pd.DataFrame(
        {
            "A": 100 * (1.001 ** np.arange(periods)),
            "B": 100 * (1.0005 ** np.arange(periods)),
            "C": 100 * (0.9998 ** np.arange(periods)),
        },
        index=index,
    )


def test_portfolio_constructors_and_constraints() -> None:
    """Equal and inverse-volatility constructors produce bounded allocations."""
    prices = _prices()
    signals = Signal(pd.DataFrame(1.0, index=prices.index, columns=prices.columns))
    constraints = AllocationConstraints(max_weight=0.6)
    equal = equal_weight(signals, constraints)
    inverse = inverse_volatility(prices, signals, window=20, constraints=constraints)
    assert np.allclose(equal.sum(axis=1), 1.0)
    assert (equal.max(axis=1) <= 0.6 + 1e-8).all()
    assert np.allclose(inverse.sum(axis=1), 1.0)
    data = MarketData(prices)
    assert EqualWeightConstructor(constraints).construct(signals, data).equals(equal)
    assert (
        InverseVolatilityConstructor(20, constraints).construct(signals, data).shape
        == prices.shape
    )


def test_allocation_constraints_reject_infeasible_bounds() -> None:
    """Impossible allocation bounds fail rather than silently violating limits."""
    prices = _prices(5)
    weights = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
    with pytest.raises(AllocationError, match="max_weight"):
        apply_constraints(weights, AllocationConstraints(max_weight=0.2))
    with pytest.raises(AllocationError, match="min_weight"):
        apply_constraints(weights, AllocationConstraints(min_weight=0.4))
    signed = pd.DataFrame({"A": [1.0] * 5, "B": [-1.0] * 5}, index=prices.index)
    bounded_signed = apply_constraints(
        signed,
        AllocationConstraints(long_only=False, max_gross_leverage=1.5),
    )
    assert bounded_signed.abs().sum(axis=1).iloc[0] == pytest.approx(1.5)


def test_risk_contributions_and_risk_parity() -> None:
    """Risk contribution utilities return aligned fractional contributions."""
    covariance = pd.DataFrame(
        [[0.04, 0.01], [0.01, 0.01]], index=["A", "B"], columns=["A", "B"]
    )
    contributions = risk_contributions(
        pd.Series([0.5, 0.5], index=["A", "B"]), covariance
    )
    weights = risk_parity_weights(covariance)
    assert contributions.sum() == pytest.approx(1.0)
    assert weights.sum() == pytest.approx(1.0)
    assert weights.index.tolist() == ["A", "B"]
    assert contributions.index.equals(weights.index)


def test_backtest_rebalances_and_applies_costs() -> None:
    """The engine produces deterministic monthly trades and cost deductions."""
    prices = _prices()
    weights = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    weights["A"] = 1.0
    config = BacktestConfig(
        rebalance_frequency="M",
        cost_model=CostModel(transaction_cost_bps=10, slippage_bps=5),
        use_vectorbt=False,
    )
    engine = VectorBTResearchEngine(config)
    result = engine.run(prices, weights, strategy_name="unit", universe_history=[])
    assert result.returns.index.equals(prices.index)
    assert result.equity_curve.iloc[-1] > 1.0
    assert result.trades["rebalance"].sum() > 1
    assert result.metrics.turnover == pytest.approx(result.trades["turnover"].sum())
    assert result.metadata["backend"] == "pandas"
    assert result.to_dict()["strategy"] == "unit"


def test_backtest_vectorbt_backend_is_available_or_falls_back() -> None:
    """The named engine exposes a deterministic backend result."""
    prices = _prices(80)
    weights = equal_weight_weights(prices)
    result = VectorBTResearchEngine(BacktestConfig()).run(prices, weights, universe_history=[])
    assert result.metadata["backend"] in {"vectorbt", "pandas"}
    assert len(result.returns) == len(prices)


def test_backtest_volatility_targeting_hook() -> None:
    """Volatility targeting scales later rebalance weights within leverage bounds."""
    prices = _prices(180)
    weights = equal_weight_weights(prices)
    engine = VectorBTResearchEngine(
        BacktestConfig(
            rebalance_frequency="M",
            volatility_target=0.1,
            volatility_lookback=20,
            max_leverage=1.0,
            use_vectorbt=False,
        )
    )
    result = engine.run(prices, weights, universe_history=[])
    assert result.weights.abs().sum(axis=1).max() <= 1.0 + 1e-8


def test_metrics_and_benchmark_suite_are_standardized() -> None:
    """Metrics and all required benchmarks use the same price/cost inputs."""
    prices = _prices()
    returns = prices["A"].pct_change().fillna(0.0)
    curve = equity_curve(returns)
    assert curve.iloc[-1] > 1.0
    assert drawdown(curve).min() <= 0
    assert rolling_sharpe(returns, window=20).notna().any()
    metrics = compute_performance_metrics(returns)
    assert metrics.observations == len(returns)
    assert metrics.to_dict()["total_return"] == pytest.approx(curve.iloc[-1] - 1)

    strategy_weights = equal_weight_weights(prices)
    config = BacktestConfig(use_vectorbt=False, cost_model=CostModel(10, 10))
    engine = VectorBTResearchEngine(config)
    benchmarks = benchmark_suite(prices, strategy_weights, engine=engine)
    assert tuple(benchmarks) == BENCHMARK_NAMES
    comparison = compare_results(
        {"strategy": engine.run(prices, strategy_weights, universe_history=[]), **benchmarks}
    )
    assert "sharpe" in comparison.columns
    assert len(comparison) == 6


def test_benchmark_weight_builders_are_deterministic() -> None:
    """Baseline weights are aligned and random weights are reproducible."""
    prices = _prices(40)
    hold = buy_and_hold_weights(prices)
    equal = equal_weight_weights(prices)
    inverse = inverse_volatility_weights(prices, window=5)
    random_one = random_weights(prices, seed=7)
    random_two = random_weights(prices, seed=7)
    persistence = persistence_weights(equal)
    assert hold.iloc[0].sum() == pytest.approx(1.0)
    assert hold.iloc[-1].equals(hold.iloc[0])
    assert np.allclose(equal.sum(axis=1), 1)
    assert np.allclose(inverse.sum(axis=1), 1)
    assert random_one.equals(random_two)
    assert persistence.equals(equal)


def test_invalid_backtest_inputs_are_rejected() -> None:
    """Engine and metric helpers fail clearly for misaligned or invalid data."""
    prices = _prices(5)
    weights = equal_weight_weights(prices)
    with pytest.raises(ResearchInputError):
        VectorBTResearchEngine().run(prices.iloc[::-1], weights.iloc[::-1], universe_history=[])
    with pytest.raises(ResearchInputError):
        compute_performance_metrics(
            pd.Series([-2.0], index=pd.date_range("2024-01-01", periods=1))
        )
