"""Reusable quantitative research contracts, factors, strategies, and universes."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .contracts import (
    CostModel,
    Experiment,
    Factor,
    FactorMetadata,
    MarketData,
    PortfolioConstructor,
    ResearchInputError,
    Signal,
    Strategy,
)
from .factors import (
    ATRFactor,
    BollingerDeviationFactor,
    EMAFactor,
    Momentum1MFactor,
    Momentum3MFactor,
    Momentum6MFactor,
    Momentum12MFactor,
    MomentumFactor,
    MovingAverageCrossoverFactor,
    RelativeStrengthRankFactor,
    RollingVolatilityFactor,
    SMAFactor,
    ZScoreFactor,
    standard_factor_set,
)
from .strategies import (
    CrossoverStrategy,
    FactorStrategy,
    MeanReversionStrategy,
    MomentumQualityStrategy,
    MomentumStrategy,
    strategy_from_name,
)
from .universe import Universe, custom_universe, nifty_50, nifty_100, resolve_universe

_LAZY_EXPORTS = {
    "ExperimentManager": ("research.experiments", "ExperimentManager"),
    "ExperimentRecord": ("research.experiments", "ExperimentRecord"),
    "ExperimentTrackingError": ("research.experiments", "ExperimentTrackingError"),
    "ResearchReport": ("research.reporting", "ResearchReport"),
    "ResearchRun": ("research.runner", "ResearchRun"),
    "generate_report": ("research.reporting", "generate_report"),
    "run_strategy": ("research.runner", "run_strategy"),
}


def __getattr__(name: str) -> Any:
    """Load cross-layer research utilities lazily to avoid package cycles."""
    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


__all__ = [
    "ATRFactor",
    "BollingerDeviationFactor",
    "CostModel",
    "CrossoverStrategy",
    "EMAFactor",
    "Experiment",
    "ExperimentManager",
    "ExperimentRecord",
    "ExperimentTrackingError",
    "Factor",
    "FactorMetadata",
    "FactorStrategy",
    "MarketData",
    "MeanReversionStrategy",
    "Momentum1MFactor",
    "MomentumQualityStrategy",
    "Momentum12MFactor",
    "Momentum3MFactor",
    "Momentum6MFactor",
    "MomentumFactor",
    "MomentumStrategy",
    "MovingAverageCrossoverFactor",
    "PortfolioConstructor",
    "ResearchInputError",
    "ResearchReport",
    "ResearchRun",
    "RelativeStrengthRankFactor",
    "RollingVolatilityFactor",
    "SMAFactor",
    "Signal",
    "Strategy",
    "Universe",
    "ZScoreFactor",
    "custom_universe",
    "generate_report",
    "nifty_100",
    "nifty_50",
    "resolve_universe",
    "run_strategy",
    "standard_factor_set",
    "strategy_from_name",
]
