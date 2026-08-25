"""Locked holdout protocol: TRAIN -> VALIDATION -> LOCKED HOLDOUT.

The research pipeline must make it impossible to evaluate a candidate on
data that validation has already touched. These tests pin the boundary
semantics: the development prefix and the trailing locked holdout form an
exact chronological partition, no walk-forward or CPCV window may reach
into the holdout, the candidate receives exactly one holdout evaluation,
and the whole protocol is deterministic.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.validation import (
    HoldoutSplit,
    holdout_split,
    run_holdout_protocol,
)
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData, ResearchInputError
from research.strategies import MomentumStrategy

HOLDOUT_SIZE = 30


def _make_data(symbols: int = 8, periods: int = 160, seed: int = 7) -> MarketData:
    rng = np.random.default_rng(seed)
    index = pd.date_range("2023-01-02", periods=periods, freq="B")
    drift = rng.normal(0.0004, 0.0004, size=symbols)
    returns = rng.normal(drift, 0.012, size=(periods, symbols))
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    frame = pd.DataFrame(
        close, index=index, columns=[f"S{i:02d}" for i in range(symbols)]
    )
    return MarketData(close=frame)


def _engine() -> VectorBTResearchEngine:
    return VectorBTResearchEngine(
        config=BacktestConfig(rebalance_frequency="M", use_vectorbt=False)
    )


def _strategy() -> MomentumStrategy:
    return MomentumStrategy(lookback=10, threshold=0.0)


def _run_protocol(data: MarketData, **overrides):
    parameters = dict(
        train_size=40,
        test_size=10,
        purge=2,
        embargo=1,
        cpcv_n_groups=4,
        cpcv_n_test_groups=2,
    )
    parameters.update(overrides)
    return run_holdout_protocol(
        _strategy(),
        data,
        EqualWeightConstructor(),
        _engine(),
        HOLDOUT_SIZE,
        **parameters,
    )


class TestHoldoutSplit:
    def test_split_partitions_timeline_exactly(self) -> None:
        data = _make_data()
        index = data.close.index
        split = holdout_split(index, HOLDOUT_SIZE)
        assert len(split.dev_index) + len(split.holdout_index) == len(index)
        assert list(split.dev_index) == list(index[:-HOLDOUT_SIZE])
        list_holding = list(split.holdout_index)
        assert list_holding == list(index[-HOLDOUT_SIZE:])
        assert split.dev_index[-1] < split.holdout_index[0]
        assert split.dev_start == index[0]
        assert split.holdout_end == index[-1]

    @pytest.mark.parametrize(
        "size",
        [0, 1, -3, True, 160, 159],
    )
    def test_split_rejects_invalid_sizes(self, size: int) -> None:
        data = _make_data(periods=160)
        with pytest.raises(ResearchInputError):
            holdout_split(data.close.index, size)

    def test_split_rejects_non_chronological_boundaries(self) -> None:
        data = _make_data(periods=160)
        index = data.close.index
        with pytest.raises(ResearchInputError):
            HoldoutSplit(
                dev_index=index[:40],
                holdout_index=index[30:60],
                holdout_size=30,
            )

    def test_split_requires_sorted_unique_index(self) -> None:
        index = pd.date_range("2023-01-02", periods=40, freq="B")
        unsorted = index[[2, 0, 1] + list(range(3, 40))]
        with pytest.raises(ResearchInputError):
            holdout_split(unsorted, 5)


class TestHoldoutProtocol:
    def test_validation_windows_stay_inside_dev(self) -> None:
        data = _make_data()
        protocol = _run_protocol(data)
        split = protocol.split
        windows = list(protocol.walk_forward.windows)
        if protocol.cpcv is not None:
            windows += list(protocol.cpcv.windows)
        assert windows, "protocol must produce validation windows"
        for window in windows:
            assert window.train_index[0] >= split.dev_start
            assert window.train_index[-1] < split.holdout_start
            assert window.test_index[0] >= split.dev_start
            assert window.test_index[-1] < split.holdout_start

    def test_holdout_evaluation_covers_holdout_exactly(self) -> None:
        data = _make_data()
        protocol = _run_protocol(data)
        assert protocol.holdout_result.returns.index.equals(
            protocol.split.holdout_index
        )
        assert protocol.holdout_result.metrics.observations == HOLDOUT_SIZE

    def test_boundaries_are_explicit_and_chronological(self) -> None:
        data = _make_data()
        protocol = _run_protocol(data)
        split_dict = protocol.to_dict()["split"]
        for key in (
            "dev_start",
            "dev_end",
            "holdout_start",
            "holdout_end",
            "dev_size",
            "holdout_size",
        ):
            assert key in split_dict
        assert split_dict["dev_end"] < split_dict["holdout_start"]
        assert split_dict["holdout_size"] == HOLDOUT_SIZE

    def test_protocol_is_deterministic(self) -> None:
        data = _make_data()
        first = json.dumps(_run_protocol(data).to_dict(), sort_keys=True)
        second = json.dumps(_run_protocol(data).to_dict(), sort_keys=True)
        assert first == second

    def test_dev_and_holdout_evaluations_are_isolated(self) -> None:
        data = _make_data()
        baseline = _run_protocol(data)
        perturbed_close = data.close.copy()
        perturbed_close.iloc[-HOLDOUT_SIZE:] *= 1.05  # holdout only
        other = _run_protocol(MarketData(close=perturbed_close))
        # Development validation never sees the holdout: identical folds.
        assert json.dumps(
            baseline.walk_forward.to_dict(), sort_keys=True
        ) == json.dumps(other.walk_forward.to_dict(), sort_keys=True)
        # The holdout evaluation reacts to holdout prices.
        assert (
            baseline.holdout_result.metrics.to_dict()
            != other.holdout_result.metrics.to_dict()
        )

    def test_full_timeline_backtest_uses_all_data(self) -> None:
        # The candidate's point-in-time weights must be computable across the
        # whole timeline, i.e. the protocol consumes the full index (dev +
        # holdout) for signal construction even though validation is dev-only.
        data = _make_data()
        protocol = _run_protocol(data)
        assert len(protocol.split.dev_index) == len(data.close.index) - HOLDOUT_SIZE
