"""Phase 2 / final-suite test 11: backtests are deterministic.

Same data + parameters + seed + code version must produce reproducible
results. A regression snapshot pins the metric vector for the reference
dataset; any change to data handling, factor math, cost model, or engine
semantics changes the snapshot and must be reviewed explicitly.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData
from research.factors import MomentumFactor
from research.strategies import FactorStrategy

#: Pinned snapshot of the reference experiment's metrics.
#: Dataset: synthetic 12-asset panel (seed 7, 420 business days from
#: 2022-01-03). Strategy: 63-day momentum, equal-weight construction.
#: Config: monthly rebalance, base India cost model, pandas backend.
REFERENCE_SNAPSHOT_SHA256 = (
    "3c0cda066cbbb4e062bcc91f76a6be869a3ac9cc7a50ea2849d4228976dafa17"
)


def make_reference_data(periods: int = 420, n: int = 12, seed: int = 7) -> pd.DataFrame:
    """Deterministic synthetic price panel (no network, no wall clock)."""
    rng = np.random.default_rng(seed)
    index = pd.date_range("2022-01-03", periods=periods, freq="B")
    columns = [f"S{i:02d}" for i in range(n)]
    drift = rng.normal(0.0003, 0.0004, size=n)
    vol = rng.uniform(0.01, 0.025, size=n)
    returns = rng.normal(drift, vol, size=(periods, n))
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(close, index=index, columns=columns)


def _run_reference(use_vectorbt: bool = False) -> "object":
    prices = make_reference_data()
    strategy = FactorStrategy(MomentumFactor(63), strategy_name="momentum63")
    data = MarketData(close=prices)
    signals = strategy.generate_signals(data)
    weights = EqualWeightConstructor().construct(signals, data)
    engine = VectorBTResearchEngine(
        BacktestConfig(
            use_vectorbt=use_vectorbt,
            cost_model=IndiaCostModel(scenario="base"),
            rebalance_frequency="M",
        )
    )
    return engine.run(prices, weights, strategy_name="momentum63")


class TestBacktestDeterminism:
    def test_same_inputs_produce_identical_metrics(self) -> None:
        first = _run_reference()
        second = _run_reference()
        assert first.metrics.to_dict() == second.metrics.to_dict()
        pd.testing.assert_series_equal(first.returns, second.returns)

    def test_equity_curve_snapshot(self) -> None:
        """Regression snapshot: the metric vector is pinned bit-for-bit."""
        result = _run_reference()
        payload = json.dumps(result.metrics.to_dict(), sort_keys=True)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert digest == REFERENCE_SNAPSHOT_SHA256, (
            "backtest snapshot changed — review the regression before updating "
            f"the constant (metrics: {result.metrics.to_dict()})"
        )

    def test_random_baseline_is_seed_deterministic(self) -> None:
        from backtest.benchmarks import random_weights

        prices = make_reference_data()
        first = random_weights(prices, seed=11)
        second = random_weights(prices, seed=11)
        other = random_weights(prices, seed=12)
        pd.testing.assert_frame_equal(first, second)
        assert not first.equals(other)

    @pytest.mark.skipif(
        not VectorBTResearchEngine().vectorbt_available,
        reason="vectorbt backend unavailable in this environment",
    )
    def test_vectorbt_backend_is_deterministic_within_environment(self) -> None:
        first = _run_reference(use_vectorbt=True)
        second = _run_reference(use_vectorbt=True)
        pd.testing.assert_series_equal(first.returns, second.returns, check_names=False)

    def test_seed_change_changes_random_baseline_only(self) -> None:
        """The reference pipeline itself has no randomness: same result."""
        prices_a = make_reference_data(seed=7)
        prices_b = make_reference_data(seed=7)
        pd.testing.assert_frame_equal(prices_a, prices_b)
        other = make_reference_data(seed=8)
        assert not prices_a.equals(other)
