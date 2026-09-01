"""Deterministic benchmark portfolios and standardized comparisons."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from portfolio.construction import inverse_volatility
from research.contracts import ResearchInputError

from .engine import BacktestConfig, BacktestResult, VectorBTResearchEngine

BENCHMARK_NAMES = (
    "buy_and_hold",
    "equal_weight",
    "inverse_volatility",
    "random",
    "persistence",
)


def _validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ResearchInputError("prices must be a non-empty DataFrame")
    if not isinstance(prices.index, pd.DatetimeIndex) or not prices.index.is_unique:
        raise ResearchInputError("prices must use a unique DatetimeIndex")
    if not prices.index.is_monotonic_increasing or not prices.columns.is_unique:
        raise ResearchInputError("prices index must be sorted and columns unique")
    numeric = prices.apply(pd.to_numeric, errors="coerce").ffill().bfill()
    if (
        numeric.isna().any().any()
        or (numeric <= 0).any().any()
        or not np.isfinite(numeric.to_numpy()).all()
    ):
        raise ResearchInputError(
            "prices must be finite and strictly positive numeric values"
        )
    return numeric.astype(float)


def buy_and_hold_weights(prices: pd.DataFrame) -> pd.DataFrame:
    """Return equal initial allocations held without subsequent rebalancing."""
    validated = _validate_prices(prices)
    weights = pd.DataFrame(np.nan, index=validated.index, columns=validated.columns)
    weights.iloc[0] = 1.0 / len(validated.columns)
    return weights.ffill().fillna(0.0)


def equal_weight_weights(prices: pd.DataFrame) -> pd.DataFrame:
    """Return equal weights on every date for a rebalancing engine."""
    validated = _validate_prices(prices)
    return pd.DataFrame(
        1.0 / len(validated.columns), index=validated.index, columns=validated.columns
    )


def inverse_volatility_weights(prices: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Return inverse-volatility benchmark weights using prior returns."""
    validated = _validate_prices(prices)
    return inverse_volatility(validated, window=window)


def random_weights(prices: pd.DataFrame, seed: int = 42) -> pd.DataFrame:
    """Return deterministic positive random weights normalized per date."""
    validated = _validate_prices(prices)
    if not isinstance(seed, int):
        raise ResearchInputError("seed must be an integer")
    generator = np.random.default_rng(seed)
    values = generator.random((len(validated.index), len(validated.columns)))
    values /= values.sum(axis=1, keepdims=True)
    return pd.DataFrame(values, index=validated.index, columns=validated.columns)


def persistence_weights(strategy_weights: pd.DataFrame) -> pd.DataFrame:
    """Carry the last available strategy allocation forward unchanged."""
    if not isinstance(strategy_weights, pd.DataFrame) or strategy_weights.empty:
        raise ResearchInputError("strategy_weights must be a non-empty DataFrame")
    return strategy_weights.ffill().fillna(0.0)


def benchmark_suite(
    prices: pd.DataFrame,
    strategy_weights: pd.DataFrame,
    *,
    engine: VectorBTResearchEngine | None = None,
    config: BacktestConfig | None = None,
    random_seed: int = 42,
    inverse_volatility_window: int = 20,
) -> dict[str, BacktestResult]:
    """Run all required benchmarks with the same engine and cost assumptions."""
    validated = _validate_prices(prices)
    if not validated.index.equals(
        strategy_weights.index
    ) or not validated.columns.equals(strategy_weights.columns):
        raise ResearchInputError("prices and strategy_weights must align")
    runner = engine or VectorBTResearchEngine(config=config)
    benchmark_weights = {
        "buy_and_hold": buy_and_hold_weights(validated),
        "equal_weight": equal_weight_weights(validated),
        "inverse_volatility": inverse_volatility_weights(
            validated, window=inverse_volatility_window
        ),
        "random": random_weights(validated, seed=random_seed),
        "persistence": persistence_weights(strategy_weights),
    }
    return {
        name: runner.run(validated, weights, strategy_name=name, universe_history=[])
        for name, weights in benchmark_weights.items()
    }


def compare_results(results: Mapping[str, BacktestResult]) -> pd.DataFrame:
    """Create a standardized metric table from strategy and benchmark results."""
    if not results:
        raise ResearchInputError("at least one backtest result is required")
    rows = []
    for name, result in results.items():
        row = {"name": name, **result.metrics.to_dict()}
        rows.append(row)
    return pd.DataFrame(rows).set_index("name")
