"""Deterministic technical and cross-sectional factor implementations."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt

import pandas as pd

from .contracts import Factor, FactorMetadata, MarketData, ResearchInputError


def _validate_window(value: int, field_name: str = "window") -> None:
    if not isinstance(value, int) or value < 2:
        raise ResearchInputError(f"{field_name} must be an integer of at least two")


def _validate_positive(value: float, field_name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ResearchInputError(f"{field_name} must be finite and greater than zero")


@dataclass(frozen=True, slots=True)
class MomentumFactor(Factor):
    """Trailing close-to-close momentum over a configurable trading window."""

    lookback: int = 21
    label: str | None = None

    def __post_init__(self) -> None:
        _validate_window(self.lookback, "lookback")

    @property
    def metadata(self) -> FactorMetadata:
        """Return momentum metadata and its reproducible parameters."""
        name = f"momentum_{self.label or f'{self.lookback}d'}"
        return FactorMetadata(
            name=name,
            family="momentum",
            description="Close return over a trailing trading-day lookback.",
            parameters={"lookback": self.lookback},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute trailing close momentum without look-ahead data."""
        return data.close.pct_change(periods=self.lookback)


class Momentum1MFactor(MomentumFactor):
    """One-month momentum using 21 trading days."""

    def __init__(self) -> None:
        super().__init__(lookback=21, label="1m")


class Momentum3MFactor(MomentumFactor):
    """Three-month momentum using 63 trading days."""

    def __init__(self) -> None:
        super().__init__(lookback=63, label="3m")


class Momentum6MFactor(MomentumFactor):
    """Six-month momentum using 126 trading days."""

    def __init__(self) -> None:
        super().__init__(lookback=126, label="6m")


class Momentum12MFactor(MomentumFactor):
    """Twelve-month momentum using 252 trading days."""

    def __init__(self) -> None:
        super().__init__(lookback=252, label="12m")


@dataclass(frozen=True, slots=True)
class SMAFactor(Factor):
    """Simple moving average of close prices."""

    window: int = 20

    def __post_init__(self) -> None:
        _validate_window(self.window)

    @property
    def metadata(self) -> FactorMetadata:
        """Return simple moving-average metadata."""
        return FactorMetadata(
            name="sma",
            family="trend",
            description="Simple moving average of close prices.",
            parameters={"window": self.window},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute a trailing simple moving average."""
        return data.close.rolling(self.window, min_periods=self.window).mean()


@dataclass(frozen=True, slots=True)
class EMAFactor(Factor):
    """Exponentially weighted moving average of close prices."""

    span: int = 20

    def __post_init__(self) -> None:
        _validate_window(self.span, "span")

    @property
    def metadata(self) -> FactorMetadata:
        """Return exponential moving-average metadata."""
        return FactorMetadata(
            name="ema",
            family="trend",
            description="Exponentially weighted moving average of close prices.",
            parameters={"span": self.span},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute an exponentially weighted trailing average."""
        return data.close.ewm(
            span=self.span, adjust=False, min_periods=self.span
        ).mean()


@dataclass(frozen=True, slots=True)
class MovingAverageCrossoverFactor(Factor):
    """Long signal where a fast moving average exceeds a slow average."""

    fast_window: int = 20
    slow_window: int = 50
    method: str = "sma"

    def __post_init__(self) -> None:
        _validate_window(self.fast_window, "fast_window")
        _validate_window(self.slow_window, "slow_window")
        if self.fast_window >= self.slow_window:
            raise ResearchInputError("fast_window must be less than slow_window")
        if self.method not in {"sma", "ema"}:
            raise ResearchInputError("method must be sma or ema")

    @property
    def metadata(self) -> FactorMetadata:
        """Return crossover metadata and parameters."""
        return FactorMetadata(
            name="moving_average_crossover",
            family="trend",
            description="Binary signal for fast average above slow average.",
            parameters={
                "fast_window": self.fast_window,
                "slow_window": self.slow_window,
                "method": self.method,
            },
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute a binary crossover panel with unavailable periods as zero."""
        if self.method == "sma":
            fast = data.close.rolling(
                self.fast_window, min_periods=self.fast_window
            ).mean()
            slow = data.close.rolling(
                self.slow_window, min_periods=self.slow_window
            ).mean()
        else:
            fast = data.close.ewm(
                span=self.fast_window, adjust=False, min_periods=self.fast_window
            ).mean()
            slow = data.close.ewm(
                span=self.slow_window, adjust=False, min_periods=self.slow_window
            ).mean()
        available = fast.notna() & slow.notna()
        return fast.gt(slow).astype(float).where(available)


@dataclass(frozen=True, slots=True)
class ZScoreFactor(Factor):
    """Rolling price z-score for mean-reversion research."""

    window: int = 20

    def __post_init__(self) -> None:
        _validate_window(self.window)

    @property
    def metadata(self) -> FactorMetadata:
        """Return z-score metadata."""
        return FactorMetadata(
            name="zscore",
            family="mean_reversion",
            description="Distance of close from its rolling mean in rolling standard deviations.",
            parameters={"window": self.window},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute rolling z-scores using trailing observations only."""
        mean = data.close.rolling(self.window, min_periods=self.window).mean()
        std = data.close.rolling(self.window, min_periods=self.window).std()
        return (data.close - mean) / std.replace(0, pd.NA)


@dataclass(frozen=True, slots=True)
class BollingerDeviationFactor(Factor):
    """Normalized distance from a rolling Bollinger-band center line."""

    window: int = 20
    num_std: float = 2.0

    def __post_init__(self) -> None:
        _validate_window(self.window)
        _validate_positive(self.num_std, "num_std")

    @property
    def metadata(self) -> FactorMetadata:
        """Return Bollinger deviation metadata."""
        return FactorMetadata(
            name="bollinger_deviation",
            family="mean_reversion",
            description="Close deviation from rolling mean scaled by Bollinger band width.",
            parameters={"window": self.window, "num_std": self.num_std},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute standardized Bollinger deviation."""
        mean = data.close.rolling(self.window, min_periods=self.window).mean()
        std = data.close.rolling(self.window, min_periods=self.window).std()
        return (data.close - mean) / (self.num_std * std).replace(0, pd.NA)


@dataclass(frozen=True, slots=True)
class RollingVolatilityFactor(Factor):
    """Annualized trailing close-return volatility."""

    window: int = 20
    annualization: int = 252

    def __post_init__(self) -> None:
        _validate_window(self.window)
        if self.annualization < 1:
            raise ResearchInputError("annualization must be positive")

    @property
    def metadata(self) -> FactorMetadata:
        """Return rolling volatility metadata."""
        return FactorMetadata(
            name="rolling_volatility",
            family="volatility",
            description="Annualized standard deviation of trailing close returns.",
            parameters={"window": self.window, "annualization": self.annualization},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute annualized trailing volatility."""
        returns = data.close.pct_change()
        return returns.rolling(self.window, min_periods=self.window).std() * sqrt(
            self.annualization
        )


@dataclass(frozen=True, slots=True)
class ATRFactor(Factor):
    """Average true range from aligned high, low, and close panels."""

    window: int = 14

    def __post_init__(self) -> None:
        _validate_window(self.window)

    @property
    def metadata(self) -> FactorMetadata:
        """Return average true range metadata."""
        return FactorMetadata(
            name="atr",
            family="volatility",
            description="Rolling mean of true range from high, low, and prior close.",
            parameters={"window": self.window},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute trailing average true range."""
        if data.high is None or data.low is None:
            raise ResearchInputError("ATR requires high and low panels")
        previous_close = data.close.shift(1)
        true_range = pd.concat(
            [
                data.high - data.low,
                (data.high - previous_close).abs(),
                (data.low - previous_close).abs(),
            ],
            keys=["high_low", "high_previous_close", "low_previous_close"],
            axis=0,
        )
        true_range = true_range.groupby(level=1).max()
        return true_range.rolling(self.window, min_periods=self.window).mean()


@dataclass(frozen=True, slots=True)
class RelativeStrengthRankFactor(Factor):
    """Cross-sectional percentile rank of trailing momentum."""

    lookback: int = 21

    def __post_init__(self) -> None:
        _validate_window(self.lookback, "lookback")

    @property
    def metadata(self) -> FactorMetadata:
        """Return relative-strength ranking metadata."""
        return FactorMetadata(
            name="relative_strength_rank",
            family="relative_strength",
            description="Cross-sectional percentile rank of trailing close momentum.",
            parameters={"lookback": self.lookback},
        )

    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute per-date percentile ranks, with strongest assets ranked highest."""
        momentum = data.close.pct_change(self.lookback)
        return momentum.rank(axis=1, pct=True, method="first")


def standard_factor_set() -> tuple[Factor, ...]:
    """Return the default factor set used by research examples."""
    return (
        Momentum1MFactor(),
        Momentum3MFactor(),
        Momentum6MFactor(),
        Momentum12MFactor(),
        SMAFactor(),
        EMAFactor(),
        ZScoreFactor(),
        BollingerDeviationFactor(),
        RollingVolatilityFactor(),
        ATRFactor(),
        RelativeStrengthRankFactor(),
    )
