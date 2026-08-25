"""Phase 2 regression tests for the production research engine.

These are deterministic: fixed synthetic data + fixed seed must produce
identical results every run and across commits. Covers factor correctness,
the production momentum+quality strategy, baselines, statistical
validation, MLflow logging, and universe date-safety.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from backtest.benchmarks import benchmark_suite, compare_results
from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.validation import deflated_sharpe_from_returns
from portfolio.construction import InverseVolatilityConstructor
from research.contracts import Experiment, MarketData, ResearchInputError
from research.experiments import ExperimentManager
from research.factors import MomentumFactor, SharpeMomentumFactor
from research.runner import run_strategy
from research.strategies import MomentumQualityStrategy
from research.universe import (
    build_universe_from_dataset,
    ensure_universe_period_covers,
)

NAMES = ["W1", "W2", "W3", "M1", "M2", "M3", "F1", "F2", "F3", "L1", "L2", "L3"]
DRIFT = {
    "W1": 0.0020,
    "W2": 0.0019,
    "W3": 0.0018,
    "M1": 0.0006,
    "M2": 0.0005,
    "M3": 0.0004,
    "F1": 0.0,
    "F2": -0.0001,
    "F3": -0.0002,
    "L1": -0.0012,
    "L2": -0.0013,
    "L3": -0.0014,
}


def make_prices(periods: int = 756, seed: int = 20260824) -> pd.DataFrame:
    index = pd.date_range("2023-01-02", periods=periods, freq="B")
    rng = np.random.default_rng(seed)
    returns = np.zeros((periods, len(NAMES)))
    for j, name in enumerate(NAMES):
        returns[:, j] = DRIFT[name] + rng.normal(0, 0.013, periods)
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(close, index=index, columns=NAMES)


def make_fundamentals(prices: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for day in prices.index[::63]:
        for name in NAMES:
            if name.startswith("W"):
                roe, debt = 0.30, 0.2
            elif name.startswith("M"):
                roe, debt = 0.25, 1.8
            else:
                roe, debt = 0.10, 1.5
            rows.append(
                {"date": day, "symbol": name, "roe": roe, "debt_to_equity": debt}
            )
    return pd.DataFrame(rows)


def build_engine() -> VectorBTResearchEngine:
    cost = IndiaCostModel(scenario="base")
    config = BacktestConfig(
        rebalance_frequency="M",
        initial_cash=1_000_000.0,
        cost_model=cost,
        volatility_target=None,
        use_vectorbt=False,
    )
    return VectorBTResearchEngine(config)


def build_strategy(prices: pd.DataFrame) -> MomentumQualityStrategy:
    return MomentumQualityStrategy(
        momentum_lookback=63,
        momentum_quantile=0.25,
        quality_quantile=0.5,
        fundamentals=make_fundamentals(prices),
    )


def _dataset() -> MarketData:
    return MarketData(close=make_prices())


class TestFactorCorrectness:
    def test_momentum_factor_value(self) -> None:
        close = pd.DataFrame(
            {"A": [100.0, 100.0, 121.0], "B": [100.0, 100.0, 90.0]},
            index=pd.date_range("2024-01-01", periods=3),
        )
        values = MomentumFactor(lookback=2).compute(MarketData(close=close))
        assert values.iloc[0].isna().all()
        assert values.iloc[2]["A"] == pytest.approx(0.21)
        assert values.iloc[2]["B"] == pytest.approx(-0.10)

    def test_sharpe_momentum_scales_by_volatility(self) -> None:
        close = pd.DataFrame(
            {
                "A": [100, 110, 120, 130, 140, 150],
                "B": [100, 110, 120, 130, 140, 150],
            },
            index=pd.date_range("2024-01-01", periods=6),
        )
        values = SharpeMomentumFactor(lookback=2, vol_window=3).compute(
            MarketData(close=close)
        )
        # Same raw momentum, identical volatility -> identical scaled value.
        assert values.iloc[-1]["A"] == pytest.approx(values.iloc[-1]["B"])


class TestProductionStrategy:
    def test_strategy_beats_baselines(self) -> None:
        data = _dataset()
        strategy = build_strategy(data.close)
        constructor = InverseVolatilityConstructor(window=20)
        engine = build_engine()
        weights = constructor.construct(strategy.generate_signals(data), data)
        result = engine.run(
            data.close, weights, strategy_name=strategy.name, universe_history=[]
        )
        benchmarks = benchmark_suite(data.close, weights, engine=engine)
        comparison = compare_results({strategy.name: result, **benchmarks})
        candidates = {
            strategy.name,
            "buy_and_hold",
            "equal_weight",
            "inverse_volatility",
            "random",
        }
        for name in candidates - {strategy.name}:
            assert result.metrics.total_return > comparison.loc[name, "total_return"], (
                name
            )
            assert result.metrics.sharpe >= comparison.loc[name, "sharpe"], name

    def test_all_benchmarks_present(self) -> None:
        data = _dataset()
        strategy = build_strategy(data.close)
        constructor = InverseVolatilityConstructor(window=20)
        engine = build_engine()
        weights = constructor.construct(strategy.generate_signals(data), data)
        engine.run(
            data.close, weights, strategy_name=strategy.name, universe_history=[]
        )
        benchmarks = benchmark_suite(data.close, weights, engine=engine)
        assert set(benchmarks) == {
            "buy_and_hold",
            "equal_weight",
            "inverse_volatility",
            "random",
            "persistence",
        }

    def test_deterministic_output(self) -> None:
        """Same data + seed -> identical metrics across runs and commits."""
        data = _dataset()
        strategy = build_strategy(data.close)
        constructor = InverseVolatilityConstructor(window=20)
        engine = build_engine()
        weights = constructor.construct(strategy.generate_signals(data), data)
        first = engine.run(
            data.close, weights, strategy_name=strategy.name, universe_history=[]
        )
        second = engine.run(
            data.close, weights, strategy_name=strategy.name, universe_history=[]
        )
        assert first.metrics.to_dict() == second.metrics.to_dict()
        assert first.returns.tolist() == second.returns.tolist()

    def test_stats_are_finite(self) -> None:
        data = _dataset()
        strategy = build_strategy(data.close)
        constructor = InverseVolatilityConstructor(window=20)
        engine = build_engine()
        weights = constructor.construct(strategy.generate_signals(data), data)
        result = engine.run(
            data.close, weights, strategy_name=strategy.name, universe_history=[]
        )
        metrics = result.metrics
        assert all(np.isfinite(v) for v in metrics.to_dict().values())
        assert metrics.sortino >= 0
        assert metrics.win_rate is not None and 0 <= metrics.win_rate <= 1


class TestStatisticalValidation:
    def test_deflated_sharpe_bounded(self) -> None:
        data = _dataset()
        strategy = build_strategy(data.close)
        constructor = InverseVolatilityConstructor(window=20)
        engine = build_engine()
        weights = constructor.construct(strategy.generate_signals(data), data)
        result = engine.run(
            data.close, weights, strategy_name=strategy.name, universe_history=[]
        )
        oos = result.returns.loc[result.returns.index[-252] :]
        dsr = deflated_sharpe_from_returns(oos, trials=6)
        assert 0 <= dsr.probability <= 1
        assert dsr.observations == 252
        assert np.isfinite(dsr.expected_max_sharpe)


class TestExperimentLogging:
    def test_mlflow_logging_records_fingerprints(self, tmp_path) -> None:
        data = _dataset()
        strategy = build_strategy(data.close)
        engine = build_engine()
        run = run_strategy(strategy, data, engine=engine, random_seed=20260824)
        manager = ExperimentManager(
            experiment_name="test-engine",
            tracking_dir=tmp_path / "experiments",
            mlflow_module=None,
            minimum_deflated_sharpe_probability=0.0 + 1e-9,
        )
        experiment = Experiment(
            hypothesis_id="HYP-ENGINE-0001",
            strategy=strategy.name,
            parameters=strategy.parameters,
            factor_set=["momentum_3m", "quality_composite"],
            universe="synthetic",
            dataset_version="synthetic-v1",
        )
        record = manager.log_experiment(
            experiment,
            result=run.result,
            validation={"dsr": 0.9},
            benchmarks=run.benchmarks,
            rejected=False,
        )
        assert record.status == "accepted"
        loaded = manager.list_records()
        assert len(loaded) == 1
        assert loaded[0].hypothesis_id == "HYP-ENGINE-0001"
        assert loaded[0].strategy == strategy.name

    def test_run_strategy_includes_benchmarks(self) -> None:
        data = _dataset()
        strategy = build_strategy(data.close)
        engine = build_engine()
        run = run_strategy(strategy, data, engine=engine, random_seed=20260824)
        assert run.comparison() is not None
        assert "buy_and_hold" in run.benchmarks


class TestUniverseSafety:
    def test_run_strategy_refuses_invalid_universe_dates(self) -> None:
        from data.universe import load_universe_dataset

        dataset = load_universe_dataset()
        universe = build_universe_from_dataset(dataset, "nifty100")
        with pytest.raises(ResearchInputError, match="no membership before"):
            # Force an earlier backtest start than the dataset records.
            ensure_universe_period_covers(universe, date(2000, 1, 1), None)

    def test_ensure_universe_covers_valid_period(self) -> None:
        from data.universe import load_universe_dataset

        dataset = load_universe_dataset()
        universe = build_universe_from_dataset(dataset, "nifty100")
        ensure_universe_period_covers(universe, date(2023, 1, 2), date(2023, 12, 31))
