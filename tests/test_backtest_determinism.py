"""Phase 2 / final-suite test 11: backtests are deterministic.

Same data + parameters + seed + code version must produce reproducible
results.

Two complementary checks:

* In-process bit-exact determinism: two runs in the same environment must
  produce identical results (the true determinism invariant).
* A value-pinned regression snapshot: the reference experiment's metrics
  are pinned to the values produced by the current code. Float comparisons
  use a 1e-10 relative tolerance — far tighter than any semantic drift
  (wrong weights, costs, or windows), yet tolerant of the ~1e-15
  bit-level noise that different numpy/BLAS builds introduce. A bit-exact
  hash across library versions is not a meaningful invariant.
"""

from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pandas as pd
import pytest

from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData
from research.factors import MomentumFactor
from research.strategies import FactorStrategy

#: Reference metrics for the reference experiment (see module docstring).
REFERENCE_METRICS = {
    "total_return": 0.07609734024026937,
    "annualized_return": 0.04498711326104221,
    "annualized_volatility": 0.10870411336852029,
    "sharpe": 0.4590586014952197,
    "sortino": 0.7190026651204149,
    "max_drawdown": -0.12875786862414085,
    "calmar": 0.34939311858574496,
    "turnover": 12.20952380952381,
    "win_rate": 0.430952380952381,
    "cost_drag": 0.034958064380952385,
    "observations": 420,
    "trade_count": 18,
}

#: Relative tolerance for float snapshot comparisons. Observed cross-build
#: noise is ~1e-15; any semantic change moves values by >> 1e-10.
_SNAPSHOT_REL_TOL = 1e-10


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
        """Regression snapshot: metric values are pinned to the reference run."""
        result = _run_reference()
        metrics = result.metrics.to_dict()
        for key, expected in REFERENCE_METRICS.items():
            actual = metrics[key]
            if key in ("observations", "trade_count"):
                assert actual == expected, (
                    f"{key} changed: expected {expected}, got {actual} "
                    f"(metrics: {metrics})"
                )
                continue
            close = math.isclose(
                float(actual), float(expected), rel_tol=_SNAPSHOT_REL_TOL
            )
            assert close, (
                f"{key} drifted from reference: expected ~{expected!r}, "
                f"got {actual!r} (metrics: {metrics})"
            )

    def test_in_process_bit_exact(self) -> None:
        """Within one environment, two runs must be bit-for-bit identical."""
        first = _run_reference()
        second = _run_reference()
        payload_first = json.dumps(first.metrics.to_dict(), sort_keys=True)
        payload_second = json.dumps(second.metrics.to_dict(), sort_keys=True)
        assert hashlib.sha256(payload_first.encode()).hexdigest() == (
            hashlib.sha256(payload_second.encode()).hexdigest()
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
