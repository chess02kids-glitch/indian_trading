"""Reusable factor-backed strategy signal generators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .contracts import Factor, MarketData, ResearchInputError, Signal, Strategy
from .factors import (
    BollingerDeviationFactor,
    MomentumFactor,
    MovingAverageCrossoverFactor,
    ZScoreFactor,
)


@dataclass(frozen=True, slots=True)
class FactorStrategy(Strategy):
    """Convert a factor panel into a long-only thresholded signal."""

    factor: Factor
    strategy_name: str = "factor"
    threshold: float = 0.0

    @property
    def name(self) -> str:
        """Return the configured strategy name."""
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Return strategy and factor parameters for experiment tracking."""
        return {"threshold": self.threshold, **self.factor.metadata.parameters}

    def generate_signals(self, data: MarketData) -> Signal:
        """Compute the factor and retain values above the long threshold."""
        values = self.factor.compute(data)
        signals = values.where(values > self.threshold, 0.0)
        return Signal(
            signals,
            metadata={"strategy": self.name, "factor": self.factor.metadata.to_dict()},
        )


@dataclass(frozen=True, slots=True)
class MomentumStrategy(Strategy):
    """Long-only strategy using positive trailing momentum."""

    lookback: int = 63
    threshold: float = 0.0

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ResearchInputError("lookback must be at least two")

    @property
    def name(self) -> str:
        """Return the stable momentum strategy name."""
        return "momentum"

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Return momentum parameters."""
        return {"lookback": self.lookback, "threshold": self.threshold}

    def generate_signals(self, data: MarketData) -> Signal:
        """Generate positive trailing-momentum signals."""
        factor = MomentumFactor(self.lookback)
        values = factor.compute(data)
        signals = values.where(values > self.threshold, 0.0)
        return Signal(
            signals,
            metadata={"strategy": self.name, "factor": factor.metadata.to_dict()},
        )


@dataclass(frozen=True, slots=True)
class CrossoverStrategy(Strategy):
    """Long-only strategy based on a moving-average crossover."""

    fast_window: int = 20
    slow_window: int = 50
    method: str = "sma"

    @property
    def name(self) -> str:
        """Return the stable crossover strategy name."""
        return "crossover"

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Return crossover parameters."""
        return {
            "fast_window": self.fast_window,
            "slow_window": self.slow_window,
            "method": self.method,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        """Generate binary crossover signals."""
        factor = MovingAverageCrossoverFactor(self.fast_window, self.slow_window, self.method)
        return Signal(
            factor.compute(data),
            metadata={"strategy": self.name, "factor": factor.metadata.to_dict()},
        )


@dataclass(frozen=True, slots=True)
class MeanReversionStrategy(Strategy):
    """Long-only strategy that activates when a price is below a z-score limit."""

    window: int = 20
    entry_zscore: float = -1.0
    bollinger: bool = False

    def __post_init__(self) -> None:
        if self.entry_zscore >= 0:
            raise ResearchInputError("entry_zscore must be negative for a long-only strategy")

    @property
    def name(self) -> str:
        """Return the stable mean-reversion strategy name."""
        return "mean_reversion"

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Return mean-reversion parameters."""
        return {
            "window": self.window,
            "entry_zscore": self.entry_zscore,
            "bollinger": self.bollinger,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        """Generate positive signals when the selected deviation is sufficiently low."""
        factor = (
            BollingerDeviationFactor(self.window) if self.bollinger else ZScoreFactor(self.window)
        )
        values = factor.compute(data)
        signals = (-values).where(values < self.entry_zscore, 0.0)
        return Signal(
            signals,
            metadata={"strategy": self.name, "factor": factor.metadata.to_dict()},
        )


def strategy_from_name(
    name: str,
    parameters: Mapping[str, Any] | None = None,
) -> Strategy:
    """Construct a supported research strategy from a configuration name."""
    normalized = name.strip().lower().replace("-", "_")
    values = dict(parameters or {})
    try:
        if normalized == "momentum":
            return MomentumStrategy(
                lookback=int(values.get("lookback", 63)),
                threshold=float(values.get("threshold", 0.0)),
            )
        if normalized == "crossover":
            return CrossoverStrategy(
                fast_window=int(values.get("fast_window", 20)),
                slow_window=int(values.get("slow_window", 50)),
                method=str(values.get("method", "sma")),
            )
        if normalized in {"mean_reversion", "meanreversion"}:
            return MeanReversionStrategy(
                window=int(values.get("window", 20)),
                entry_zscore=float(values.get("entry_zscore", -1.0)),
                bollinger=bool(values.get("bollinger", False)),
            )
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(f"invalid parameters for strategy {name!r}") from exc
    raise ResearchInputError(f"unsupported strategy: {name}")
