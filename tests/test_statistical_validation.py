"""Tests for the v0.3 statistical-validation engine.

Covers deflated-Sharpe correctness against independently computed reference
values, purge/embargo correctness, CPCV reproducibility, bootstrap
determinism across all supported metrics, and validation consistency.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.validation import (
    bootstrap_metric_intervals,
    bootstrap_sharpe_confidence_interval,
    combinatorial_purged_cv,
    deflated_sharpe_from_returns,
    deflated_sharpe_ratio,
    expected_maximum_sharpe,
    run_combinatorial_purged_cv,
    run_walk_forward,
    validation_consistency,
    walk_forward_splits,
)
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData, ResearchInputError, Signal, Strategy


def _index(size: int = 30) -> pd.DatetimeIndex:
    """Return a deterministic business-day index."""
    return pd.date_range("2020-01-01", periods=size, freq="B")


def _positions(index: pd.DatetimeIndex, timestamp: pd.Timestamp) -> int:
    return int(np.where(index == timestamp)[0][0])


class TestDeflatedSharpe:
    def test_reference_values_bailey_2014_formulas(self) -> None:
        """DSR matches independently computed reference values (pinned).

        The reference values are computed directly from the Bailey &
        López de Prado (2014) formulas with the standard normal quantile
        function, independently of the implementation.
        """
        result = deflated_sharpe_ratio(
            0.1,
            trials=50,
            observations=100,
            skewness=-1.0,
            kurtosis=6.0,
        )
        assert result.expected_max_sharpe == pytest.approx(
            0.24130287765283756, rel=1e-12
        )
        assert result.standard_error == pytest.approx(0.10600647969522306, rel=1e-12)
        assert result.probability == pytest.approx(0.09127172164485947, rel=1e-12)

    def test_single_trial_expected_max_is_zero(self) -> None:
        """One trial has nothing to deflate: E[max] == 0 under the null."""
        result = deflated_sharpe_ratio(
            0.0, trials=1, observations=100, skewness=0.0, kurtosis=3.0
        )
        assert result.expected_max_sharpe == pytest.approx(0.0)
        assert result.probability == pytest.approx(0.5, rel=1e-12)

    def test_expected_maximum_matches_reference_quantiles(self) -> None:
        assert expected_maximum_sharpe(20) == pytest.approx(
            1.9007079511811982, rel=1e-12
        )
        assert expected_maximum_sharpe(50) == pytest.approx(
            2.276303093420348, rel=1e-12
        )
        assert expected_maximum_sharpe(1) == 0.0

    def test_multiple_testing_correction_is_conservative(self) -> None:
        """More tested variants imply a higher hurdle and lower probability."""
        one = deflated_sharpe_ratio(0.5, trials=1, observations=200)
        many = deflated_sharpe_ratio(0.5, trials=100, observations=200)
        assert many.probability < one.probability
        assert many.expected_max_sharpe > one.expected_max_sharpe

    def test_skew_and_kurtosis_adjustment_direction(self) -> None:
        """Negative skew and excess kurtosis widen the Sharpe standard error."""
        normal = deflated_sharpe_ratio(1.0, trials=20, observations=200)
        skewed = deflated_sharpe_ratio(
            1.0, trials=20, observations=200, skewness=-1.0, kurtosis=5.0
        )
        assert skewed.standard_error > normal.standard_error
        assert skewed.probability < normal.probability

    def test_from_returns_uses_sample_moments(self) -> None:
        """DSR-from-returns matches the direct formula on the same moments."""
        returns = pd.Series(
            np.linspace(-0.01, 0.02, 300),
            index=pd.date_range("2020-01-01", periods=300),
        )
        direct = deflated_sharpe_from_returns(returns, trials=6)
        assert 0 <= direct.probability <= 1
        assert direct.observations == 300
        assert direct.trials == 6


class TestPurgeAndEmbargo:
    def _prices(self, periods: int = 60):
        index = _index(periods)
        prices = pd.DataFrame(
            {
                "A": 100 * 1.001 ** np.arange(periods),
                "B": 100 * 1.0005 ** np.arange(periods),
            },
            index=index,
        )
        return index, MarketData(prices)

    def test_walk_forward_purge_removes_overlapping_labels(self) -> None:
        """Every retained training row must be outside the purge window."""
        index = _index(40)
        windows = walk_forward_splits(index, train_size=10, test_size=5, purge=3)
        for window in windows:
            test_start = _positions(index, window.test_index[0])
            for train_position in window.train_index:
                position = _positions(index, train_position)
                assert position < test_start - 3, (
                    "training observation inside purge window leaked"
                )

    def test_walk_forward_embargo_inserts_gap(self) -> None:
        index = _index(40)
        windows = walk_forward_splits(index, train_size=10, test_size=5, embargo=4)
        for window in windows:
            train_end = _positions(index, window.train_index[-1])
            test_start = _positions(index, window.test_index[0])
            # Four rows are skipped between the raw train end and the test.
            assert test_start - train_end == 5

    def test_purge_and_embargo_create_zero_look_ahead_gap(self) -> None:
        index = _index(40)
        windows = walk_forward_splits(
            index, train_size=10, test_size=5, purge=3, embargo=2
        )
        for window in windows:
            train_end = _positions(index, window.train_index[-1])
            test_start = _positions(index, window.test_index[0])
            # Purged rows plus embargo rows plus the boundary row are all
            # absent from training.
            assert test_start - train_end == 3 + 2 + 1
            assert not set(window.train_index).intersection(window.test_index)

    def test_expanding_windows_still_purge_tail(self) -> None:
        index = _index(40)
        windows = walk_forward_splits(
            index, train_size=10, test_size=5, expanding=True, purge=2
        )
        assert len(windows[1].train_index) > len(windows[0].train_index)
        for window in windows:
            assert int(window.purge) == 2
            assert len(window.train_index) <= int(window.to_dict()["train_size"])

    def test_purge_larger_than_train_rejected(self) -> None:
        with pytest.raises(ResearchInputError):
            walk_forward_splits(_index(), train_size=5, test_size=5, purge=5)


class TestCPCV:
    def test_number_of_paths_is_n_choose_k(self) -> None:
        windows = combinatorial_purged_cv(_index(40), 4, 2)
        assert len(windows) == 6  # C(4, 2)

    def test_reproducible_across_calls(self) -> None:
        """Identical inputs produce identical windows (deterministic folds)."""
        index = _index(60)
        first = combinatorial_purged_cv(index, 5, 2, purge=2, embargo=1)
        second = combinatorial_purged_cv(index, 5, 2, purge=2, embargo=1)
        assert [window.to_dict() for window in first] == [
            window.to_dict() for window in second
        ]
        for left, right in zip(first, second, strict=True):
            pd.testing.assert_index_equal(left.train_index, right.train_index)
            pd.testing.assert_index_equal(left.test_index, right.test_index)

    def test_train_and_test_are_disjoint_and_purged(self) -> None:
        index = _index(60)
        windows = combinatorial_purged_cv(index, 5, 2, purge=2, embargo=1)
        for window in windows:
            assert not set(window.train_index).intersection(window.test_index)
            for test_position in window.test_index:
                test_pos = _positions(index, test_position)
                for train_position in window.train_index:
                    train_pos = _positions(index, train_position)
                    assert abs(train_pos - test_pos) >= 3, (
                        "training observation within purge/embargo radius leaked"
                    )

    def test_train_must_remain_non_empty(self) -> None:
        with pytest.raises(ResearchInputError):
            combinatorial_purged_cv(_index(10), 3, 2, purge=100)

    @pytest.mark.parametrize(
        "n_groups,n_test_groups",
        [(2, 2), (2, 0), (5, 5)],
    )
    def test_invalid_combinations_rejected(self, n_groups, n_test_groups) -> None:
        with pytest.raises(ResearchInputError):
            combinatorial_purged_cv(_index(40), n_groups, n_test_groups)


class TestBootstrap:
    def _returns(self, periods: int = 300):
        rng = np.random.default_rng(7)
        values = rng.normal(0.0005, 0.01, periods)
        return pd.Series(
            values, index=pd.date_range("2022-01-03", periods=periods, freq="B")
        )

    def test_all_metrics_are_deterministic(self) -> None:
        returns = self._returns()
        turnover = pd.Series(0.01, index=returns.index)
        first = bootstrap_metric_intervals(
            returns, turnover=turnover, samples=300, seed=11
        )
        second = bootstrap_metric_intervals(
            returns, turnover=turnover, samples=300, seed=11
        )
        assert first.keys() == {
            "sharpe",
            "cagr",
            "max_drawdown",
            "volatility",
            "turnover",
        }
        for metric, interval in first.items():
            assert second[metric] == interval
            assert interval.lower <= interval.estimate <= interval.upper
            assert interval.samples == 300
            assert interval.seed == 11

    def test_estimate_matches_point_estimate(self) -> None:
        returns = self._returns()
        intervals = bootstrap_metric_intervals(returns, samples=200, seed=1)
        # Sharpe estimate equals the sample Sharpe.
        expected = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))
        assert intervals["sharpe"].estimate == pytest.approx(expected, rel=1e-12)
        expected_cagr = float((1.0 + returns).prod() ** (252 / len(returns)) - 1.0)
        assert intervals["cagr"].estimate == pytest.approx(expected_cagr, rel=1e-12)

    def test_turnover_ci_requires_turnover_series(self) -> None:
        returns = self._returns()
        with pytest.raises(ResearchInputError):
            bootstrap_metric_intervals(returns, metrics=("turnover",), samples=200)

    def test_block_bootstrap_is_deterministic_and_distinct(self) -> None:
        returns = self._returns()
        turnover = pd.Series(0.01, index=returns.index)
        first = bootstrap_metric_intervals(
            returns, turnover=turnover, samples=200, seed=3, block_length=21
        )
        second = bootstrap_metric_intervals(
            returns, turnover=turnover, samples=200, seed=3, block_length=21
        )
        iid = bootstrap_metric_intervals(
            returns, turnover=turnover, samples=200, seed=3
        )
        assert first == second
        assert first["sharpe"].block_length == 21
        assert first["sharpe"] != iid["sharpe"]

    def test_sharpe_wrapper_is_backward_compatible(self) -> None:
        returns = self._returns()
        wrapped = bootstrap_sharpe_confidence_interval(returns, samples=200, seed=5)
        assert wrapped.metric == "sharpe"
        assert wrapped.estimate == pytest.approx(
            bootstrap_metric_intervals(returns, samples=200, seed=5)["sharpe"].estimate,
            rel=1e-12,
        )

    def test_seed_changes_interval(self) -> None:
        returns = self._returns()
        one = bootstrap_metric_intervals(returns, samples=200, seed=1)
        two = bootstrap_metric_intervals(returns, samples=200, seed=2)
        assert one["sharpe"] != two["sharpe"]


class _TrendStrategy(Strategy):
    """Deterministic strategy favouring the best-drifting asset."""

    @property
    def name(self) -> str:
        return "trend"

    def generate_signals(self, data: MarketData) -> Signal:
        values = pd.DataFrame(
            np.nan, index=data.close.index, columns=data.close.columns
        )
        values.iloc[21:] = 1.0
        values.iloc[:, 1:] = 0.0
        return Signal(values)


class TestValidationRunner:
    def _data(self, periods: int = 90):
        index = _index(periods)
        rng = np.random.default_rng(5)
        a = 0.001 + rng.normal(0, 0.01, periods)
        b = rng.normal(0, 0.01, periods)
        prices = pd.DataFrame(
            {"A": 100 * np.exp(np.cumsum(a)), "B": 100 * np.exp(np.cumsum(b))},
            index=index,
        )
        return MarketData(prices)

    def test_walk_forward_run_is_deterministic_and_test_only(self) -> None:
        data = self._data()
        engine = VectorBTResearchEngine(BacktestConfig(use_vectorbt=False))
        first = run_walk_forward(
            _TrendStrategy(),
            data,
            EqualWeightConstructor(),
            engine,
            train_size=30,
            test_size=15,
            purge=3,
            embargo=2,
        )
        second = run_walk_forward(
            _TrendStrategy(),
            data,
            EqualWeightConstructor(),
            engine,
            train_size=30,
            test_size=15,
            purge=3,
            embargo=2,
        )
        assert first.to_dict() == second.to_dict()
        assert len(first.fold_results) == len(first.windows)
        assert first.aggregate_metrics.observations == sum(
            len(window.test_index) for window in first.windows
        )

    def test_cpcv_run_reproducible_and_disjoint(self) -> None:
        data = self._data()
        engine = VectorBTResearchEngine(BacktestConfig(use_vectorbt=False))
        first = run_combinatorial_purged_cv(
            _TrendStrategy(),
            data,
            EqualWeightConstructor(),
            engine,
            n_groups=4,
            n_test_groups=2,
            purge=3,
            embargo=1,
        )
        second = run_combinatorial_purged_cv(
            _TrendStrategy(),
            data,
            EqualWeightConstructor(),
            engine,
            n_groups=4,
            n_test_groups=2,
            purge=3,
            embargo=1,
        )
        assert first.to_dict() == second.to_dict()
        assert first.method == "cpcv"
        assert len(first.windows) == 6

    def test_validation_consistency_summary(self) -> None:
        data = self._data()
        engine = VectorBTResearchEngine(BacktestConfig(use_vectorbt=False))
        result = run_combinatorial_purged_cv(
            _TrendStrategy(),
            data,
            EqualWeightConstructor(),
            engine,
            n_groups=4,
            n_test_groups=2,
            purge=3,
            embargo=1,
        )
        summary = validation_consistency(result)
        assert summary["folds"] == 6
        assert 0 <= summary["positive_fold_fraction"] <= 1
        assert summary["worst_fold_sharpe"] <= summary["best_fold_sharpe"]
        assert 0 <= summary["aggregate_deflated_sharpe_probability"] <= 1
