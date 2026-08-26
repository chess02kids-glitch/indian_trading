"""Deterministic strategy registry: the only way research selects strategies.

An AI agent (or any caller) may select a strategy by *registered id* with
*predefined parameter values*; it may never submit code. The registry maps
ids to code-defined factories and enforces parameter bounds. Unknown ids
and out-of-bounds parameters are rejected before any backtest runs.

The registry is built once at import time from Python code. There is no
dynamic import, no serialization of callables, and no way for untrusted
input to extend it.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import ResearchInputError, Strategy

__all__ = [
    "ParameterOutOfBoundsError",
    "RegisteredStrategy",
    "STRATEGY_REGISTRY",
    "StrategyRegistry",
    "UnknownStrategyError",
    "registry_context",
]


class UnknownStrategyError(ResearchInputError):
    """Raised when a proposal references an unregistered strategy id."""


class ParameterOutOfBoundsError(ResearchInputError):
    """Raised when a proposal uses parameter values outside allowed bounds."""


#: Registry ids whose factories accept the point-in-time fundamentals frame.
_FUNDAMENTALS_AWARE = frozenset({"quality", "momentum_quality"})


@dataclass(frozen=True, slots=True)
class RegisteredStrategy:
    """One code-defined strategy in the registry."""

    registry_id: str
    family: str
    version: str
    description: str
    canonical_parameters: Mapping[str, Any]
    parameter_bounds: Mapping[str, tuple[float, float]]
    factory: Callable[..., Strategy]

    def context(self) -> dict[str, Any]:
        """AI-facing description of this registered strategy."""
        return {
            "registry_id": self.registry_id,
            "family": self.family,
            "version": self.version,
            "description": self.description,
            "canonical_parameters": dict(self.canonical_parameters),
            "parameter_bounds": {
                name: [lower, upper]
                for name, (lower, upper) in self.parameter_bounds.items()
            },
        }


def _bounds(
    spec: Mapping[str, Any], names: tuple[str, ...]
) -> dict[str, tuple[float, float]]:
    """Extract numeric (min, max) bounds for the named integer/float params."""
    output: dict[str, tuple[float, float]] = {}
    for name in names:
        if name not in spec:
            continue
        lower = float(spec[name].get("min"))
        upper = float(spec[name].get("max"))
        if lower > upper:
            raise ResearchInputError(f"invalid bounds for parameter {name!r}")
        output[name] = (lower, upper)
    return output


def _build_registry() -> dict[str, RegisteredStrategy]:
    """Construct the code-defined strategy registry (import-time, immutable)."""
    from .strategies import (
        CrossoverStrategy,
        CrossSectionalMomentumStrategy,
        LowVolatilityStrategy,
        MeanReversionStrategy,
        MomentumQualityStrategy,
        MomentumStrategy,
        QualityRankingStrategy,
        ReversalStrategy,
        TrendFollowingStrategy,
    )

    # (registry_id, family, version, description, canonical, bounds, factory)
    specs: tuple[
        tuple[
            str, str, str, str, dict[str, Any], dict[str, Any], Callable[..., Strategy]
        ],
        ...,
    ] = (
        (
            "momentum",
            "momentum",
            "1.0",
            "Long-only positive trailing momentum, thresholded.",
            {"lookback": 63, "threshold": 0.0},
            _bounds(
                {
                    "lookback": {"min": 20, "max": 260},
                    "threshold": {"min": 0.0, "max": 0.0},
                },
                ("lookback", "threshold"),
            ),
            MomentumStrategy,
        ),
        (
            "crossover",
            "trend",
            "1.0",
            "Moving-average crossover (fast/slow), long-only.",
            {"fast_window": 20, "slow_window": 50, "method": "sma"},
            _bounds(
                {
                    "fast_window": {"min": 5, "max": 60},
                    "slow_window": {"min": 50, "max": 260},
                },
                ("fast_window", "slow_window"),
            ),
            CrossoverStrategy,
        ),
        (
            "mean_reversion",
            "mean_reversion",
            "1.0",
            "Long-only entry when price z-score falls below a negative limit.",
            {"window": 20, "entry_zscore": -1.0, "bollinger": False},
            _bounds(
                {
                    "window": {"min": 5, "max": 60},
                    "entry_zscore": {"min": -3.0, "max": -0.1},
                },
                ("window", "entry_zscore"),
            ),
            MeanReversionStrategy,
        ),
        (
            "momentum_quality",
            "momentum_quality",
            "1.0",
            "Frozen v0.6 baseline: top momentum quantile within top quality "
            "quantile (requires fundamentals). Canonical parameters are the "
            "frozen values; any deviation is a NEW experiment.",
            {
                "momentum_lookback": 63,
                "momentum_quantile": 0.25,
                "quality_quantile": 0.5,
            },
            _bounds(
                {
                    "momentum_lookback": {"min": 42, "max": 126},
                    "momentum_quantile": {"min": 0.05, "max": 0.95},
                    "quality_quantile": {"min": 0.05, "max": 0.95},
                },
                ("momentum_lookback", "momentum_quantile", "quality_quantile"),
            ),
            MomentumQualityStrategy,
        ),
        (
            "cross_sectional_momentum",
            "momentum",
            "1.0",
            "Top quantile of trailing 126-day returns, equal-weighted.",
            {"lookback": 126, "quantile": 0.25},
            _bounds(
                {
                    "lookback": {"min": 42, "max": 250},
                    "quantile": {"min": 0.05, "max": 0.5},
                },
                ("lookback", "quantile"),
            ),
            CrossSectionalMomentumStrategy,
        ),
        (
            "trend_following",
            "trend",
            "1.0",
            "Hold assets above their 200-day moving average.",
            {"slow_window": 200, "method": "sma"},
            _bounds({"slow_window": {"min": 100, "max": 250}}, ("slow_window",)),
            TrendFollowingStrategy,
        ),
        (
            "quality",
            "quality",
            "1.0",
            "Top quantile of composite fundamental quality (requires "
            "fundamentals; ROE + debt-to-equity).",
            {"quantile": 0.5},
            _bounds({"quantile": {"min": 0.05, "max": 0.95}}, ("quantile",)),
            QualityRankingStrategy,
        ),
        (
            "low_volatility",
            "volatility",
            "1.0",
            "Lowest-quantile realized volatility over 63 days.",
            {"window": 63, "quantile": 0.25},
            _bounds(
                {
                    "window": {"min": 20, "max": 120},
                    "quantile": {"min": 0.05, "max": 0.5},
                },
                ("window", "quantile"),
            ),
            LowVolatilityStrategy,
        ),
        (
            "reversal",
            "mean_reversion",
            "1.0",
            "Most oversold quantile by 20-day price z-score.",
            {"window": 20, "quantile": 0.25},
            _bounds(
                {
                    "window": {"min": 5, "max": 60},
                    "quantile": {"min": 0.05, "max": 0.5},
                },
                ("window", "quantile"),
            ),
            ReversalStrategy,
        ),
    )
    registry: dict[str, RegisteredStrategy] = {}
    for registry_id, family, version, description, canonical, bounds, factory in specs:
        registry[registry_id] = RegisteredStrategy(
            registry_id=registry_id,
            family=family,
            version=version,
            description=description,
            canonical_parameters=canonical,
            parameter_bounds=bounds,
            factory=factory,
        )
    return registry


#: Truly immutable code-defined registry (mapping proxy — mutation raises
#: TypeError at runtime, so even a bug cannot extend the registry).
STRATEGY_REGISTRY: Mapping[str, RegisteredStrategy] = types.MappingProxyType(
    _build_registry()
)


class StrategyRegistry:
    """Validation layer over :data:`STRATEGY_REGISTRY`.

    ``build`` is the single entry point for turning a proposal into a
    runnable strategy: it looks the id up in the code-defined registry,
    validates every supplied parameter against the declared bounds, fills
    canonical defaults, and only then calls the factory.
    """

    def __init__(
        self, registry: Mapping[str, RegisteredStrategy] | None = None
    ) -> None:
        self.registry = dict(registry or STRATEGY_REGISTRY)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.registry))

    def families(self) -> tuple[str, ...]:
        return tuple(sorted({entry.family for entry in self.registry.values()}))

    def get(self, registry_id: str) -> RegisteredStrategy:
        normalized = str(registry_id).strip().lower()
        try:
            return self.registry[normalized]
        except KeyError as exc:
            raise UnknownStrategyError(
                f"unknown strategy id {registry_id!r}; registered: "
                + ", ".join(self.ids())
            ) from exc

    def _validated_parameters(
        self, entry: RegisteredStrategy, parameters: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        supplied = dict(parameters or {})
        unknown = set(supplied) - set(entry.canonical_parameters)
        if unknown:
            raise ParameterOutOfBoundsError(
                f"strategy {entry.registry_id!r} does not accept parameters: "
                + ", ".join(sorted(unknown))
            )
        values = dict(entry.canonical_parameters)
        for name, raw in supplied.items():
            bounds = entry.parameter_bounds.get(name)
            if name not in entry.canonical_parameters:
                continue
            # Enum-like string parameters (method) pass through unchanged.
            if isinstance(entry.canonical_parameters[name], str):
                values[name] = str(raw)
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ParameterOutOfBoundsError(
                    f"parameter {name!r} must be numeric, got {raw!r}"
                ) from exc
            if bounds is not None:
                lower, upper = bounds
                if value < lower or value > upper:
                    raise ParameterOutOfBoundsError(
                        f"parameter {name!r}={value} outside allowed range "
                        f"[{lower}, {upper}] for strategy {entry.registry_id!r}"
                    )
            # Preserve integer-ness of canonical integer parameters.
            if isinstance(entry.canonical_parameters[name], int):
                if not float(raw).is_integer():
                    raise ParameterOutOfBoundsError(
                        f"parameter {name!r} must be an integer for "
                        f"strategy {entry.registry_id!r}"
                    )
                values[name] = int(value)
            else:
                values[name] = value
        return values

    def build(
        self,
        registry_id: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        fundamentals: Any | None = None,
        active_members: Any | None = None,
    ) -> Strategy:
        """Build a strategy from a registered id and validated parameters."""
        entry = self.get(registry_id)
        values = self._validated_parameters(entry, parameters)
        kwargs: dict[str, Any] = dict(values)
        # Only strategies that explicitly consume fundamentals receive the
        # frame; the registry is explicit about what each strategy may see.
        if entry.registry_id in _FUNDAMENTALS_AWARE and fundamentals is not None:
            kwargs["fundamentals"] = fundamentals
        if active_members is not None:
            kwargs["active_members"] = active_members
        try:
            return entry.factory(**kwargs)
        except TypeError as exc:
            raise ResearchInputError(
                f"strategy factory {entry.registry_id!r} rejected parameters: {exc}"
            ) from exc

    def context(self) -> dict[str, Any]:
        """AI-facing registry summary (ids, families, bounds)."""
        return {
            "registered_strategies": [
                entry.context() for entry in self.registry.values()
            ],
            "families": list(self.families()),
        }

    def validate_proposal(
        self, registry_id: str, parameters: Mapping[str, Any]
    ) -> None:
        """Validate a proposal without building the strategy (dry-run)."""
        entry = self.get(registry_id)
        self._validated_parameters(entry, parameters)


def registry_context() -> dict[str, Any]:
    """Return the global registry context (module-level convenience)."""
    return StrategyRegistry().context()
