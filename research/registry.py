"""Deterministic strategy registry and benchmark zoo.

Every family has one predeclared canonical configuration. This is not a
search grid. The same implementation is reused by backtest, paper, and
(later) a live adapter; signal logic never branches on execution mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from .contracts import Factor, MarketData, ResearchInputError, Signal, Strategy
from .factors import (
    MomentumFactor,
    MovingAverageCrossoverFactor,
    RollingVolatilityFactor,
    ZScoreFactor,
)
from .pit import rank_eligible

__all__ = [
    "BENCHMARK_ZOO",
    "CANONICAL_CONFIGS",
    "RegisteredStrategy",
    "StrategySpec",
    "allowed_families",
    "canonical_strategy",
    "instantiate",
    "registry_metadata",
]


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """Declarative, non-executable description of a registered family."""

    family: str
    version: str
    name: str
    description: str
    parameters: Mapping[str, Any]
    features: tuple[str, ...]
    requires_fundamentals: bool = False


class RegisteredStrategy(Strategy):
    """Cross-sectional ranking strategy with an explicit PIT mask."""

    def __init__(
        self,
        spec: StrategySpec,
        *,
        factor: Factor,
        higher_is_better: bool = True,
        quantile: float = 0.25,
        invert: bool = False,
        active_members: pd.DataFrame | None = None,
        parameters: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> None:
        if not 0 < quantile <= 1:
            raise ResearchInputError("quantile must be in (0, 1]")
        self._spec = spec
        self._factor = factor
        self._higher_is_better = higher_is_better
        self._quantile = quantile
        self._invert = invert
        self._active_members = active_members
        self._parameters = dict(parameters or spec.parameters)
        self._seed = seed

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def family(self) -> str:
        return self._spec.family

    @property
    def version(self) -> str:
        return self._spec.version

    @property
    def parameters(self) -> Mapping[str, Any]:
        payload = dict(self._parameters)
        payload["quantile"] = self._quantile
        if self._seed is not None:
            payload["seed"] = self._seed
        return payload

    def metadata(self) -> dict[str, Any]:
        return {
            "strategy_name": self.name,
            "strategy_family": self.family,
            "strategy_version": self.version,
            "parameters": dict(self.parameters),
            "feature_set": list(self._spec.features),
        }

    def generate_signals(self, data: MarketData) -> Signal:
        values = self._factor.compute(data)
        if self._invert:
            values = -values
        ranks = rank_eligible(
            values,
            self._active_members,
            ascending=not self._higher_is_better,
        )
        selected = ranks.ge(1.0 - self._quantile).where(ranks.notna())
        signals = ranks.where(selected, 0.0).fillna(0.0)
        meta = self.metadata()
        meta["factor"] = self._factor.metadata.to_dict()
        meta["signal_timestamp"] = (
            str(data.close.index[-1]) if len(data.close.index) else None
        )
        return Signal(signals, metadata=meta)


class _ConstantFactor:
    """Unit factor used by buy-and-hold / equal-weight families."""

    def __init__(self, name: str, family: str) -> None:
        from .contracts import FactorMetadata

        self._meta = FactorMetadata(
            name=name, family=family, description=name, parameters={}
        )

    @property
    def metadata(self):  # noqa: D401
        return self._meta

    def compute(self, data: MarketData) -> pd.DataFrame:
        return pd.DataFrame(1.0, index=data.close.index, columns=data.close.columns)


class _RandomFactor:
    def __init__(self, seed: int) -> None:
        from .contracts import FactorMetadata

        self.seed = seed
        self._meta = FactorMetadata(
            name="placebo_random",
            family="placebo",
            description="Seeded uniform scores; not an economic signal.",
            parameters={"seed": seed},
        )

    @property
    def metadata(self):
        return self._meta

    def compute(self, data: MarketData) -> pd.DataFrame:
        import numpy as np

        rng = np.random.default_rng(self.seed)
        values = rng.random((len(data.close.index), len(data.close.columns)))
        return pd.DataFrame(values, index=data.close.index, columns=data.close.columns)


class _PersistenceFactor:
    def __init__(self, lookback: int = 21) -> None:
        from .contracts import FactorMetadata

        self.lookback = lookback
        self._meta = FactorMetadata(
            name="persistence",
            family="persistence",
            description="Recent 1-month return as a persistence score.",
            parameters={"lookback": lookback},
        )

    @property
    def metadata(self):
        return self._meta

    def compute(self, data: MarketData) -> pd.DataFrame:
        return data.close.pct_change(self.lookback)


class _QualityProxyFactor:
    """ROE-like quality from supplied fundamentals, or NaN if absent."""

    def __init__(self, fundamentals: pd.DataFrame | None) -> None:
        from .contracts import FactorMetadata

        self.fundamentals = fundamentals
        self._meta = FactorMetadata(
            name="quality_roe_proxy",
            family="quality",
            description="Point-in-time ROE when fundamentals exist.",
            parameters={},
        )

    @property
    def metadata(self):
        return self._meta

    def compute(self, data: MarketData) -> pd.DataFrame:
        empty = pd.DataFrame(
            float("nan"), index=data.close.index, columns=data.close.columns
        )
        if self.fundamentals is None or self.fundamentals.empty:
            return empty
        frame = self.fundamentals.copy()
        if "roe" not in frame.columns:
            return empty
        frame["date"] = pd.to_datetime(frame["date"])
        panel = frame.pivot(index="date", columns="symbol", values="roe")
        panel.columns = [str(c).upper() for c in panel.columns]
        extended = data.close.index.union(pd.DatetimeIndex(panel.index))
        aligned = panel.reindex(extended).ffill().reindex(data.close.index)
        return aligned.reindex(columns=data.close.columns)


CANONICAL_CONFIGS: dict[str, StrategySpec] = {
    "buy_and_hold": StrategySpec(
        family="buy_and_hold",
        version="1.0",
        name="buy_and_hold",
        description="Equal initial allocation; no ranking edge claimed.",
        parameters={"quantile": 1.0},
        features=("close",),
    ),
    "equal_weight": StrategySpec(
        family="equal_weight",
        version="1.0",
        name="equal_weight",
        description="Equal weight of every PIT-eligible name.",
        parameters={"quantile": 1.0},
        features=("close",),
    ),
    "inverse_volatility": StrategySpec(
        family="inverse_volatility",
        version="1.0",
        name="inverse_volatility",
        description="Prefer low realized volatility names.",
        parameters={"window": 20, "quantile": 0.5},
        features=("realized_volatility",),
    ),
    "random": StrategySpec(
        family="random",
        version="1.0",
        name="random_placebo",
        description="Seeded placebo ranking. Not an economic hypothesis.",
        parameters={"seed": 20260824, "quantile": 0.25},
        features=("noise",),
    ),
    "persistence": StrategySpec(
        family="persistence",
        version="1.0",
        name="persistence",
        description="Short-horizon return continuation.",
        parameters={"lookback": 21, "quantile": 0.25},
        features=("momentum_1m",),
    ),
    "momentum": StrategySpec(
        family="momentum",
        version="1.0",
        name="cross_sectional_momentum",
        description="63-day cross-sectional momentum, monthly-style ranking.",
        parameters={"lookback": 63, "quantile": 0.25},
        features=("momentum_3m",),
    ),
    "trend": StrategySpec(
        family="trend",
        version="1.0",
        name="trend_following",
        description="200-day moving-average trend filter (fast 50 / slow 200).",
        parameters={"fast_window": 50, "slow_window": 200, "quantile": 0.5},
        features=("ma_crossover",),
    ),
    "quality": StrategySpec(
        family="quality",
        version="1.0",
        name="quality",
        description="Point-in-time ROE ranking when fundamentals exist.",
        parameters={"quantile": 0.5},
        features=("roe",),
        requires_fundamentals=True,
    ),
    "low_volatility": StrategySpec(
        family="low_volatility",
        version="1.0",
        name="low_volatility",
        description="Lowest realized-volatility names.",
        parameters={"window": 63, "quantile": 0.25},
        features=("realized_volatility",),
    ),
    "mean_reversion": StrategySpec(
        family="mean_reversion",
        version="1.0",
        name="short_term_reversal",
        description="Long names with low short-term z-score (overstretched down).",
        parameters={"window": 20, "quantile": 0.25},
        features=("zscore_20d",),
    ),
}

BENCHMARK_ZOO: tuple[str, ...] = tuple(CANONICAL_CONFIGS)


def allowed_families() -> frozenset[str]:
    return frozenset(CANONICAL_CONFIGS)


def _builder(
    family: str,
    parameters: Mapping[str, Any],
    *,
    active_members: pd.DataFrame | None,
    fundamentals: pd.DataFrame | None,
) -> RegisteredStrategy:
    spec = CANONICAL_CONFIGS[family]
    merged = {**spec.parameters, **dict(parameters)}
    quantile = float(merged.get("quantile", 0.25))
    if family in {"buy_and_hold", "equal_weight"}:
        return RegisteredStrategy(
            spec,
            factor=_ConstantFactor(family, family),
            higher_is_better=True,
            quantile=1.0,
            active_members=active_members,
            parameters=merged,
        )
    if family == "inverse_volatility":
        return RegisteredStrategy(
            spec,
            factor=RollingVolatilityFactor(int(merged.get("window", 20))),
            higher_is_better=False,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    if family == "random":
        seed = int(merged.get("seed", 20260824))
        return RegisteredStrategy(
            spec,
            factor=_RandomFactor(seed),
            higher_is_better=True,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
            seed=seed,
        )
    if family == "persistence":
        return RegisteredStrategy(
            spec,
            factor=_PersistenceFactor(int(merged.get("lookback", 21))),
            higher_is_better=True,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    if family == "momentum":
        return RegisteredStrategy(
            spec,
            factor=MomentumFactor(int(merged.get("lookback", 63))),
            higher_is_better=True,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    if family == "trend":
        return RegisteredStrategy(
            spec,
            factor=MovingAverageCrossoverFactor(
                int(merged.get("fast_window", 50)),
                int(merged.get("slow_window", 200)),
            ),
            higher_is_better=True,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    if family == "quality":
        return RegisteredStrategy(
            spec,
            factor=_QualityProxyFactor(fundamentals),
            higher_is_better=True,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    if family == "low_volatility":
        return RegisteredStrategy(
            spec,
            factor=RollingVolatilityFactor(int(merged.get("window", 63))),
            higher_is_better=False,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    if family == "mean_reversion":
        return RegisteredStrategy(
            spec,
            factor=ZScoreFactor(int(merged.get("window", 20))),
            higher_is_better=False,
            quantile=quantile,
            active_members=active_members,
            parameters=merged,
        )
    raise ResearchInputError(f"unsupported family: {family}")


_BUILDERS: dict[str, Callable[..., RegisteredStrategy]] = {
    name: _builder for name in CANONICAL_CONFIGS
}


def instantiate(
    family: str,
    parameters: Mapping[str, Any] | None = None,
    *,
    active_members: pd.DataFrame | None = None,
    fundamentals: pd.DataFrame | None = None,
) -> RegisteredStrategy:
    normalized = family.strip().lower().replace("-", "_")
    aliases = {
        "cross_sectional_momentum": "momentum",
        "trend_following": "trend",
        "placebo": "random",
        "random_placebo": "random",
        "short_term_reversal": "mean_reversion",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in CANONICAL_CONFIGS:
        raise ResearchInputError(
            f"strategy family {family!r} is not registered; "
            f"allowed: {sorted(CANONICAL_CONFIGS)}"
        )
    return _builder(
        normalized,
        parameters or {},
        active_members=active_members,
        fundamentals=fundamentals,
    )


def canonical_strategy(
    family: str,
    **kwargs: Any,
) -> RegisteredStrategy:
    return instantiate(family, CANONICAL_CONFIGS[family].parameters, **kwargs)


def registry_metadata() -> list[dict[str, Any]]:
    return [
        {
            "family": spec.family,
            "version": spec.version,
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.parameters),
            "features": list(spec.features),
            "requires_fundamentals": spec.requires_fundamentals,
        }
        for spec in CANONICAL_CONFIGS.values()
    ]
