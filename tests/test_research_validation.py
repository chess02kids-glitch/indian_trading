"""Tests for walk-forward, CPCV, DSR, and bootstrap validation utilities."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.validation import (
    bootstrap_sharpe_confidence_interval,
    combinatorial_purged_cv,
    deflated_sharpe_from_returns,
    deflated_sharpe_ratio,
    run_walk_forward,
    walk_forward_splits,
)
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData, ResearchInputError, Signal, Strategy


def _index(size: int = 30) -> pd.DatetimeIndex:
    """Return a deterministic business-day index."""
    return pd.date_range("2020-01-01", periods=size, freq="B")


def test_walk_forward_rolling_and_expanding_splits() -> None:
    """Rolling and expanding windows respect test gaps and embargo."""
    index = _index()
    rolling = walk_forward_splits(index, train_size=10, test_size=5, embargo=2)
    expanding = walk_forward_splits(index, train_size=10, test_size=5, expanding=True)
    assert rolling[0].train_index[-1] < rolling[0].test_index[0]
    assert len(rolling) == 3
    assert len(expanding[1].train_index) > len(expanding[0].train_index)
    assert rolling[0].to_dict()["embargo"] == 2


def test_combinatorial_purged_cv_has_expected_number_of_paths() -> None:
    """CPCV emits n-choose-k paths and purges observations around test groups."""
    windows = combinatorial_purged_cv(
        _index(40), n_groups=4, n_test_groups=2, embargo=1
    )
    assert len(windows) == 6
    assert all(
        not set(window.train_index).intersection(window.test_index)
        for window in windows
    )
    assert all(len(window.train_index) > 0 for window in windows)


def test_deflated_sharpe_and_bootstrap_are_deterministic() -> None:
    """Statistical corrections return bounded, reproducible outputs."""
    result = deflated_sharpe_ratio(1.0, trials=10, observations=200)
    assert 0 <= result.probability <= 1
    assert result.to_dict()["trials"] == 10
    returns = pd.Series(
        np.linspace(-0.01, 0.02, 100), index=pd.date_range("2020-01-01", periods=100)
    )
    from_returns = deflated_sharpe_from_returns(returns, trials=5)
    bootstrap_one = bootstrap_sharpe_confidence_interval(returns, samples=500, seed=11)
    bootstrap_two = bootstrap_sharpe_confidence_interval(returns, samples=500, seed=11)
    assert from_returns.observations == 100
    assert bootstrap_one == bootstrap_two
    assert bootstrap_one.lower <= bootstrap_one.estimate <= bootstrap_one.upper


def test_validation_rejects_invalid_parameters() -> None:
    """Temporal and statistical validators reject impossible configurations."""
    with pytest.raises(ResearchInputError):
        walk_forward_splits(_index(), train_size=100, test_size=5)
    with pytest.raises(ResearchInputError):
        combinatorial_purged_cv(_index(), n_groups=2, n_test_groups=2)
    with pytest.raises(ResearchInputError):
        deflated_sharpe_ratio(1, trials=0, observations=10)
    with pytest.raises(ResearchInputError):
        bootstrap_sharpe_confidence_interval(
            pd.Series([0.1], index=pd.date_range("2024-01-01", periods=1)), samples=100
        )


class _PositiveSignalStrategy(Strategy):
    """Simple deterministic strategy double for walk-forward evaluation."""

    @property
    def name(self) -> str:
        """Return the test strategy name."""
        return "positive"

    def generate_signals(self, data: MarketData) -> Signal:
        """Signal equally for every supplied asset."""
        return Signal(
            pd.DataFrame(1.0, index=data.close.index, columns=data.close.columns)
        )


def test_run_walk_forward_returns_fold_results() -> None:
    """Walk-forward runner produces test-only backtest outputs."""
    index = _index(50)
    prices = pd.DataFrame(
        {"A": 100 * 1.001 ** np.arange(50), "B": 100 * 1.0005 ** np.arange(50)},
        index=index,
    )
    data = MarketData(prices)
    result = run_walk_forward(
        _PositiveSignalStrategy(),
        data,
        EqualWeightConstructor(),
        VectorBTResearchEngine(BacktestConfig(use_vectorbt=False)),
        train_size=20,
        test_size=10,
    )
    assert len(result.windows) == 3
    assert len(result.fold_results) == 3
    assert result.aggregate_metrics.observations == 30
    assert result.to_dict()["windows"][0]["train_size"] == 20
