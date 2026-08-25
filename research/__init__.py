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
    SharpeMomentumFactor,
    SMAFactor,
    ZScoreFactor,
    standard_factor_set,
)
from .universe import Universe, custom_universe, nifty_50, nifty_100, resolve_universe

# Strategies are intentionally lazy: ``research.strategies`` imports
# ``models.quality``, and ``models.quality`` imports ``research.contracts``.
# Eagerly importing strategies from this package would make that a circular
# import that depends on module load order. Resolving them lazily keeps the
# public ``from research import MomentumQualityStrategy`` API stable.
_LAZY_EXPORTS = {
    "CrossoverStrategy": ("research.strategies", "CrossoverStrategy"),
    "FactorStrategy": ("research.strategies", "FactorStrategy"),
    "MeanReversionStrategy": ("research.strategies", "MeanReversionStrategy"),
    "MomentumQualityStrategy": ("research.strategies", "MomentumQualityStrategy"),
    "MomentumStrategy": ("research.strategies", "MomentumStrategy"),
    "strategy_from_name": ("research.strategies", "strategy_from_name"),
    "ExperimentManager": ("research.experiments", "ExperimentManager"),
    "ExperimentRecord": ("research.experiments", "ExperimentRecord"),
    "ExperimentTrackingError": ("research.experiments", "ExperimentTrackingError"),
    "build_research_artifacts": ("research.experiments", "build_research_artifacts"),
    "ResearchReport": ("research.reporting", "ResearchReport"),
    "PeriodReport": ("research.reporting", "PeriodReport"),
    "AdvancedResearchReport": ("research.reporting", "AdvancedResearchReport"),
    "generate_advanced_report": ("research.reporting", "generate_advanced_report"),
    "ResearchRun": ("research.runner", "ResearchRun"),
    "generate_periodic_reports": ("research.reporting", "generate_periodic_reports"),
    "generate_report": ("research.reporting", "generate_report"),
    "run_strategy": ("research.runner", "run_strategy"),
    "ResearchGate": ("research.gate", "ResearchGate"),
    "ResearchGateConfig": ("research.gate", "ResearchGateConfig"),
    "GateDecision": ("research.gate", "GateDecision"),
    "GateVerdict": ("research.gate", "GateVerdict"),
    "GateCheck": ("research.gate", "GateCheck"),
    "generate_placebo_results": ("research.gate", "generate_placebo_results"),
    "FactorDiagnostics": ("research.diagnostics", "FactorDiagnostics"),
    "factor_decay": ("research.diagnostics", "factor_decay"),
    "rank_stability": ("research.diagnostics", "rank_stability"),
    "sector_exposure": ("research.diagnostics", "sector_exposure"),
    "turnover_attribution": ("research.diagnostics", "turnover_attribution"),
    "volatility_contribution": ("research.diagnostics", "volatility_contribution"),
    "factor_contribution_breakdown": (
        "research.diagnostics",
        "factor_contribution_breakdown",
    ),
    "LongRunReplay": ("research.replay", "LongRunReplay"),
    "ReplaySchedule": ("research.replay", "ReplaySchedule"),
    "compute_paper_analytics": ("research.paper_analytics", "compute_paper_analytics"),
    "Fill": ("research.paper_analytics", "Fill"),
    "PositionMark": ("research.paper_analytics", "PositionMark"),
    "PaperAnalytics": ("research.paper_analytics", "PaperAnalytics"),
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
    "AdvancedResearchReport",
    "BollingerDeviationFactor",
    "CostModel",
    "CrossoverStrategy",
    "EMAFactor",
    "Experiment",
    "ExperimentManager",
    "ExperimentRecord",
    "ExperimentTrackingError",
    "Factor",
    "FactorDiagnostics",
    "FactorMetadata",
    "FactorStrategy",
    "Fill",
    "GateCheck",
    "GateDecision",
    "GateVerdict",
    "LongRunReplay",
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
    "PaperAnalytics",
    "PeriodReport",
    "PortfolioConstructor",
    "PositionMark",
    "ReplaySchedule",
    "ResearchGate",
    "ResearchGateConfig",
    "ResearchInputError",
    "ResearchReport",
    "ResearchRun",
    "RelativeStrengthRankFactor",
    "RollingVolatilityFactor",
    "SharpeMomentumFactor",
    "Signal",
    "SMAFactor",
    "Strategy",
    "Universe",
    "ZScoreFactor",
    "build_research_artifacts",
    "compute_paper_analytics",
    "custom_universe",
    "factor_contribution_breakdown",
    "factor_decay",
    "generate_advanced_report",
    "generate_periodic_reports",
    "generate_placebo_results",
    "generate_report",
    "nifty_100",
    "nifty_50",
    "rank_stability",
    "resolve_universe",
    "run_strategy",
    "sector_exposure",
    "standard_factor_set",
    "strategy_from_name",
    "turnover_attribution",
    "volatility_contribution",
]
