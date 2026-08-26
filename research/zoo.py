"""Deterministic benchmark zoo: ten economically distinct strategy families.

The zoo exists to answer one question: *under one immutable methodology,
how do economically distinct ideas compare?* Every family runs on the same
prices, the same point-in-time universe mask, the same cost model, the same
portfolio constraints (equal-weight long-only within the selection), and
the same backtest engine. Families are pre-declared with small canonical
configurations — the zoo is methodological validation, not a grid search.

Families:

1. ``buy_and_hold`` — equal initial allocation, never rebalanced.
2. ``equal_weight`` — equal weights, rebalanced at the engine frequency.
3. ``inverse_volatility`` — weights inversely proportional to prior
   20-day realized volatility.
4. ``random`` — seeded random weights (deterministic for a fixed seed).
5. ``persistence`` — the previous month's cross-sectional momentum
   selection held unchanged this month (stale-signal persistence).
6. ``cross_sectional_momentum`` — top-quantile trailing 126-day returns.
7. ``trend_following`` — assets trading above their 200-day average.
8. ``quality`` — top-quantile composite fundamental quality (requires a
   point-in-time fundamentals frame).
9. ``low_volatility`` — lowest-quantile 63-day realized volatility.
10. ``mean_reversion`` — most-oversold quantile by 20-day price z-score.

Every cross-sectional family ranks **within** the point-in-time universe
membership (mask before ranking). Weight-based families mask their weights
to members and renormalize. None of these results are evidence about real
markets; the zoo verifies the framework on data where the truth may or may
not be known.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from backtest.benchmarks import (
    buy_and_hold_weights,
    equal_weight_weights,
    inverse_volatility_weights,
    random_weights,
)
from backtest.engine import BacktestConfig, BacktestResult, VectorBTResearchEngine
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData, ResearchInputError, Signal, Strategy

from .registry import StrategyRegistry

__all__ = [
    "IdentityConstructor",
    "WeightPanelStrategy",
    "ZOO_FAMILIES",
    "zoo_context",
    "run_benchmark_zoo",
    "run_zoo_family",
]


class WeightPanelStrategy(Strategy):
    """Expose a precomputed weight panel through the Strategy interface.

    Used so weight-based zoo families (buy & hold, equal weight, inverse
    volatility, random, persistence) can run through the same validation
    machinery (walk-forward / CPCV) as signal-based families. The panel
    must be causal: every row computable from data up to that row — the
    zoo families satisfy this by construction.
    """

    def __init__(self, weights: pd.DataFrame, name: str = "weight_panel") -> None:
        self._weights = _validate_weight_panel(weights).astype(float)
        self._name = str(name)

    @property
    def name(self) -> str:
        return self._name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {"source": "precomputed_weight_panel"}

    def generate_signals(self, data: MarketData) -> Signal:
        if not isinstance(data, MarketData):
            raise ResearchInputError("generate_signals requires MarketData")
        aligned = self._weights.reindex(
            index=data.close.index, columns=data.close.columns
        )
        aligned = aligned.fillna(0.0)
        return Signal(
            aligned,
            metadata={"strategy": self._name, "source": "weight_panel"},
        )


class IdentityConstructor:
    """Return signal values unchanged as target weights.

    Counterpart of :class:`WeightPanelStrategy` for validation runs: the
    panel already contains normalized target weights.
    """

    def construct(self, signals: Signal, data: MarketData) -> pd.DataFrame:
        if not signals.values.index.equals(data.close.index):
            raise ResearchInputError("signals and market data must share an index")
        return signals.values.astype(float)


#: Pre-declared zoo families with canonical configurations. Adding a family
#: is a code change with a regression test, never a runtime action.
ZOO_FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "family_id": "buy_and_hold",
        "family": "passive",
        "canonical_parameters": {},
        "description": "equal initial allocation, never rebalanced",
    },
    {
        "family_id": "equal_weight",
        "family": "passive",
        "canonical_parameters": {},
        "description": "equal weights rebalanced at engine frequency",
    },
    {
        "family_id": "inverse_volatility",
        "family": "volatility",
        "canonical_parameters": {"window": 20},
        "description": "weights inversely proportional to prior realized volatility",
    },
    {
        "family_id": "random",
        "family": "placebo",
        "canonical_parameters": {"seed": 42},
        "description": "seeded random weights (deterministic placebo)",
    },
    {
        "family_id": "persistence",
        "family": "persistence",
        "canonical_parameters": {"source": "cross_sectional_momentum"},
        "description": "previous month's momentum selection held unchanged",
    },
    {
        "family_id": "cross_sectional_momentum",
        "family": "momentum",
        "canonical_parameters": {"lookback": 126, "quantile": 0.25},
        "description": "top-quantile trailing 126-day returns",
    },
    {
        "family_id": "trend_following",
        "family": "trend",
        "canonical_parameters": {"slow_window": 200, "method": "sma"},
        "description": "assets trading above their 200-day moving average",
    },
    {
        "family_id": "quality",
        "family": "quality",
        "canonical_parameters": {"quantile": 0.5},
        "description": "top-quantile composite fundamental quality",
    },
    {
        "family_id": "low_volatility",
        "family": "volatility",
        "canonical_parameters": {"window": 63, "quantile": 0.25},
        "description": "lowest-quantile 63-day realized volatility",
    },
    {
        "family_id": "mean_reversion",
        "family": "mean_reversion",
        "canonical_parameters": {"window": 20, "quantile": 0.25},
        "description": "most-oversold quantile by 20-day price z-score",
    },
)

#: registry id used for each zoo family's signal-based construction.
_FAMILY_REGISTRY_IDS = {
    "cross_sectional_momentum": "cross_sectional_momentum",
    "trend_following": "trend_following",
    "quality": "quality",
    "low_volatility": "low_volatility",
    "mean_reversion": "reversal",
}


def _validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(prices, pd.DataFrame) or prices.empty:
        raise ResearchInputError("prices must be a non-empty DataFrame")
    if not isinstance(prices.index, pd.DatetimeIndex) or not prices.index.is_unique:
        raise ResearchInputError("prices must use a unique DatetimeIndex")
    numeric = prices.apply(pd.to_numeric, errors="coerce")
    if (
        numeric.isna().any().any()
        or (numeric <= 0).any().any()
        or not np.isfinite(numeric.to_numpy()).all()
    ):
        raise ResearchInputError("prices must be finite and strictly positive")
    return numeric.astype(float)


def _validate_weight_panel(weights: pd.DataFrame) -> pd.DataFrame:
    """Validate a target-weight panel (finite, non-negative, unique keys)."""
    if not isinstance(weights, pd.DataFrame) or weights.empty:
        raise ResearchInputError("weights must be a non-empty DataFrame")
    if not isinstance(weights.index, pd.DatetimeIndex) or not weights.index.is_unique:
        raise ResearchInputError("weights must use a unique DatetimeIndex")
    if not weights.columns.is_unique:
        raise ResearchInputError("weights columns must be unique")
    numeric = weights.apply(pd.to_numeric, errors="coerce")
    if (
        numeric.isna().any().any()
        or (numeric < 0).any().any()
        or not np.isfinite(numeric.to_numpy()).all()
    ):
        raise ResearchInputError("weights must be finite and non-negative")
    return numeric.astype(float)


def _membership_mask(
    membership: pd.DataFrame | None,
    index: pd.DatetimeIndex,
    columns: pd.Index,
) -> pd.DataFrame:
    """Align a point-in-time membership panel; absent cells are False."""
    if membership is None:
        return pd.DataFrame(True, index=index, columns=columns)
    if not isinstance(membership, pd.DataFrame):
        raise ResearchInputError("membership must be a DataFrame or None")
    return membership.reindex(index=index, columns=columns).astype(bool).fillna(False)


def _mask_weights(weights: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Zero non-member weights and renormalize each row to sum one.

    Rows with no eligible members stay zero (the engine treats a zero
    weight as cash). This is applied to weight-based families whose
    statistics are per-symbol (buy-and-hold, equal weight, inverse
    volatility, random); cross-sectional *ranking* families mask inside
    the strategy, before ranking.
    """
    masked = weights.where(mask, 0.0).astype(float)
    row_sums = masked.sum(axis=1)
    normalized = masked.div(row_sums, axis=0)
    normalized = normalized.replace([np.inf, -np.inf], 0.0).fillna(0.0)
    return normalized


def _stale_shift(weights: pd.DataFrame) -> pd.DataFrame:
    """Shift a weight panel one month back (persistence family).

    For every date ``d`` in month *m*, the effective weights are the
    selection observed at the *last trading day of month m-1*. The engine
    samples month-end targets, so on the last trading day of month *m* the
    target is exactly the previous month's selection — a one-month stale
    portfolio held for one month.

    The mapping is month-period based, so calendar month-ends (which may
    fall on non-trading days) can never collide with the engine's
    business-day sampling.
    """
    periods = weights.index.to_period("M")
    monthly = weights.groupby(periods).tail(1)
    monthly = monthly.set_axis(monthly.index.to_period("M"))
    previous = periods - 1
    stale = monthly.reindex(previous)
    stale.index = weights.index
    return stale.fillna(0.0)


def _signal_weights(
    registry_id: str,
    prices: pd.DataFrame,
    *,
    fundamentals: pd.DataFrame | None,
    mask: pd.DataFrame,
    parameters: Mapping[str, Any],
) -> pd.DataFrame:
    """Build a zoo family's weights through the registry + equal weight."""
    registry = StrategyRegistry()
    strategy = registry.build(
        registry_id,
        parameters,
        fundamentals=fundamentals,
        active_members=mask,
    )
    data = MarketData(
        close=prices,
        high=None,
        low=None,
        volume=None,
    )
    signals = strategy.generate_signals(data)
    return EqualWeightConstructor().construct(signals, data)


def run_zoo_family(
    family_id: str,
    prices: pd.DataFrame,
    *,
    fundamentals: pd.DataFrame | None = None,
    membership: pd.DataFrame | None = None,
    engine: VectorBTResearchEngine | None = None,
    config: BacktestConfig | None = None,
    seed: int = 42,
) -> BacktestResult:
    """Run one pre-declared zoo family under the shared methodology."""
    normalized = str(family_id).strip().lower()
    entry = next(
        (item for item in ZOO_FAMILIES if item["family_id"] == normalized), None
    )
    if entry is None:
        raise ResearchInputError(
            f"unknown zoo family {family_id!r}; available: "
            + ", ".join(item["family_id"] for item in ZOO_FAMILIES)
        )
    validated = _validate_prices(prices)
    mask = _membership_mask(membership, validated.index, validated.columns)
    runner = engine or VectorBTResearchEngine(config=config)
    canonical = dict(entry["canonical_parameters"])

    if normalized == "buy_and_hold":
        weights = _mask_weights(buy_and_hold_weights(validated), mask)
        return runner.run(
            validated, weights, strategy_name=normalized, universe_history=[]
        )
    if normalized == "equal_weight":
        weights = _mask_weights(equal_weight_weights(validated), mask)
        return runner.run(
            validated, weights, strategy_name=normalized, universe_history=[]
        )
    if normalized == "inverse_volatility":
        weights = _mask_weights(
            inverse_volatility_weights(validated, window=int(canonical["window"])),
            mask,
        )
        return runner.run(
            validated, weights, strategy_name=normalized, universe_history=[]
        )
    if normalized == "random":
        weights = _mask_weights(random_weights(validated, seed=seed), mask)
        return runner.run(
            validated, weights, strategy_name=normalized, universe_history=[]
        )
    if normalized == "persistence":
        source = _signal_weights(
            _FAMILY_REGISTRY_IDS["cross_sectional_momentum"],
            validated,
            fundamentals=None,
            mask=mask,
            parameters={
                "lookback": 126,
                "quantile": 0.25,
            },
        )
        weights = _stale_shift(source)
        return runner.run(
            validated, weights, strategy_name=normalized, universe_history=[]
        )
    if normalized == "quality" and fundamentals is None:
        raise ResearchInputError(
            "zoo family 'quality' requires a point-in-time fundamentals "
            "frame (date/symbol/roe/debt_to_equity)"
        )
    registry_id = _FAMILY_REGISTRY_IDS[normalized]
    parameters = {
        key: value for key, value in canonical.items() if key not in ("seed",)
    }
    weights = _signal_weights(
        registry_id,
        validated,
        fundamentals=fundamentals,
        mask=mask,
        parameters=parameters,
    )
    return runner.run(validated, weights, strategy_name=normalized, universe_history=[])


def run_benchmark_zoo(
    prices: pd.DataFrame,
    *,
    fundamentals: pd.DataFrame | None = None,
    membership: pd.DataFrame | None = None,
    engine: VectorBTResearchEngine | None = None,
    config: BacktestConfig | None = None,
    seed: int = 42,
    families: tuple[str, ...] | None = None,
) -> dict[str, BacktestResult]:
    """Run every pre-declared zoo family and return results keyed by id."""
    selected = (
        tuple(families)
        if families
        else tuple(item["family_id"] for item in ZOO_FAMILIES)
    )
    results: dict[str, BacktestResult] = {}
    failures: list[str] = []
    for family_id in selected:
        try:
            results[family_id] = run_zoo_family(
                family_id,
                prices,
                fundamentals=fundamentals,
                membership=membership,
                engine=engine,
                config=config,
                seed=seed,
            )
        except ResearchInputError:
            # A family that cannot run on this data (e.g. quality without
            # fundamentals) is reported by the caller; the zoo continues so
            # one blocked family never hides the others.
            failures.append(family_id)
    if failures and not results:
        raise ResearchInputError(
            "no zoo families could run; failed: " + ", ".join(failures)
        )
    return results


def zoo_context() -> dict[str, Any]:
    """AI-facing description of the pre-declared zoo."""
    return {
        "zoo_families": list(ZOO_FAMILIES),
        "methodology": (
            "one engine, one cost model, one rebalance frequency, "
            "mask-before-rank point-in-time universe handling, "
            "canonical parameters only"
        ),
    }
