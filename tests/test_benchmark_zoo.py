"""Benchmark zoo tests: ten families under one shared methodology."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestConfig, VectorBTResearchEngine
from research.contracts import ResearchInputError
from research.zoo import (
    ZOO_FAMILIES,
    run_benchmark_zoo,
    run_zoo_family,
    zoo_context,
)


def make_world(n_symbols: int = 12, n_days: int = 480, seed: int = 7) -> pd.DataFrame:
    """Deterministic geometric random-walk price panel."""
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-02", periods=n_days)
    columns = [f"SYM{i:02d}" for i in range(n_symbols)]
    log_returns = generator.normal(0.0004, 0.015, size=(n_days, n_symbols))
    prices = 100.0 * np.exp(np.cumsum(log_returns, axis=0))
    return pd.DataFrame(prices, index=index, columns=columns)


def make_fundamentals(prices: pd.DataFrame) -> pd.DataFrame:
    """Quarterly point-in-time fundamentals for every symbol."""
    rows = []
    for symbol in prices.columns:
        dates = pd.date_range(prices.index[0], prices.index[-1], freq="QE")
        for i, date in enumerate(dates):
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "roe": 0.08 + 0.02 * (i % 5),
                    "debt_to_equity": 0.3 + 0.05 * (i % 4),
                }
            )
    return pd.DataFrame(rows)


class TestZooShape:
    def test_ten_families_predeclared(self) -> None:
        ids = [entry["family_id"] for entry in ZOO_FAMILIES]
        assert len(ids) == 10
        assert ids == [
            "buy_and_hold",
            "equal_weight",
            "inverse_volatility",
            "random",
            "persistence",
            "cross_sectional_momentum",
            "trend_following",
            "quality",
            "low_volatility",
            "mean_reversion",
        ]

    def test_unknown_family_rejected(self) -> None:
        prices = make_world()
        with pytest.raises(ResearchInputError):
            run_zoo_family("alchemy", prices)

    def test_context_describes_zoo(self) -> None:
        context = zoo_context()
        assert len(context["zoo_families"]) == 10
        assert "mask-before-rank" in context["methodology"]


class TestZooRuns:
    def test_all_families_run_and_return_valid_results(self) -> None:
        prices = make_world(n_days=520)
        fundamentals = make_fundamentals(prices)
        results = run_benchmark_zoo(prices, fundamentals=fundamentals)
        assert set(results) == {entry["family_id"] for entry in ZOO_FAMILIES}
        for family_id, result in results.items():
            assert result.strategy_name == family_id
            assert len(result.returns) == len(prices)
            assert np.isfinite(result.returns.to_numpy()).all()
            assert result.metrics is not None

    def test_quality_requires_fundamentals(self) -> None:
        prices = make_world()
        with pytest.raises(ResearchInputError):
            run_zoo_family("quality", prices)

    def test_deterministic_across_runs(self) -> None:
        prices = make_world()
        first = run_benchmark_zoo(prices, seed=42)
        second = run_benchmark_zoo(prices, seed=42)
        for family_id in first:
            assert first[family_id].returns.equals(second[family_id].returns)

    def test_seed_changes_random_family_only(self) -> None:
        prices = make_world()
        seeded_a = run_benchmark_zoo(prices, seed=1)
        seeded_b = run_benchmark_zoo(prices, seed=2)
        assert not seeded_a["random"].returns.equals(seeded_b["random"].returns)
        assert seeded_a["buy_and_hold"].returns.equals(seeded_b["buy_and_hold"].returns)

    def test_membership_respected_mask_before_rank(self) -> None:
        prices = make_world(n_symbols=10, n_days=520)
        membership = pd.DataFrame(True, index=prices.index, columns=prices.columns)
        # SYM05 is a member only after 2024-06-01.
        cutoff = pd.Timestamp("2024-06-03")
        membership.loc[membership.index < cutoff, "SYM05"] = False
        results = run_benchmark_zoo(prices, membership=membership)
        csm = results["cross_sectional_momentum"]
        # The engine samples month-end targets; on dates before the cutoff
        # SYM05 must never carry weight.
        pre = csm.weights.loc[csm.weights.index < cutoff, "SYM05"]
        assert (pre == 0.0).all()

    def test_single_family_runner(self) -> None:
        prices = make_world(n_days=520)
        result = run_zoo_family("low_volatility", prices)
        assert result.strategy_name == "low_volatility"

    def test_shared_engine_and_costs(self) -> None:
        prices = make_world(n_days=520)
        config = BacktestConfig(initial_cash=1_000_000.0)
        engine = VectorBTResearchEngine(config=config)
        results = run_benchmark_zoo(
            prices,
            fundamentals=make_fundamentals(prices),
            engine=engine,
            config=config,
        )
        for family_id, result in results.items():
            assert result.metadata["initial_cash"] == 1_000_000.0
            assert result.metadata["rebalance_frequency"] == "M"

    def test_persistence_is_stale_momentum(self) -> None:
        prices = make_world(n_days=520)
        results = run_benchmark_zoo(prices)
        momentum = results["cross_sectional_momentum"].weights
        persistence = results["persistence"].weights
        # Persistence holds the previous month's momentum selection: at
        # every month-end target, persistence equals the momentum weights
        # observed at the prior month's last trading day.
        periods = momentum.index.to_period("M")
        monthly = momentum.groupby(periods).tail(1)
        monthly = monthly.set_axis(monthly.index.to_period("M"))
        expected_daily = monthly.reindex(periods - 1)
        expected_daily.index = momentum.index
        expected_daily = expected_daily.fillna(0.0)
        # Month-end targets: the last business day of each month (the
        # engine's `~duplicated(keep="last")` convention).
        rebalance = np.asarray(~momentum.index.to_period("M").duplicated(keep="last"))
        rebalance[0] = False
        # The constructor renormalizes via apply_constraints, so compare
        # with tolerance rather than exact equality.
        assert np.allclose(
            persistence.to_numpy()[rebalance],
            expected_daily.to_numpy()[rebalance],
            atol=1e-9,
        )
