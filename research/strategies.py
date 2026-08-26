"""Reusable factor-backed strategy signal generators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from models.quality import CompositeQualityFactor, DebtQualityFactor, RoeQualityFactor

from .contracts import Factor, MarketData, ResearchInputError, Signal, Strategy
from .factors import (
    BollingerDeviationFactor,
    EMAFactor,
    MomentumFactor,
    MovingAverageCrossoverFactor,
    RollingVolatilityFactor,
    SMAFactor,
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
        factor = MovingAverageCrossoverFactor(
            self.fast_window, self.slow_window, self.method
        )
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
            raise ResearchInputError(
                "entry_zscore must be negative for a long-only strategy"
            )

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
            BollingerDeviationFactor(self.window)
            if self.bollinger
            else ZScoreFactor(self.window)
        )
        values = factor.compute(data)
        signals = (-values).where(values < self.entry_zscore, 0.0)
        return Signal(
            signals,
            metadata={"strategy": self.name, "factor": factor.metadata.to_dict()},
        )


@dataclass(frozen=True, slots=True)
class MomentumQualityStrategy(Strategy):
    """Cross-sectional momentum + quality screen (the baseline strategy).

    Long-only, rebalanced by the backtest engine (monthly in the standard
    configuration). Each date keeps the top ``momentum_quantile`` of assets
    by trailing momentum, further restricted to the top ``quality_quantile``
    by the composite fundamental quality score. Quality observations are
    point-in-time: the latest available fundamental date is forward-filled,
    never a future one.

    ``active_members`` is an optional point-in-time universe mask (boolean
    date x symbol panel). When supplied, the cross-sectional quantile
    screens rank *only the symbols that were index members on that date*
    (the v0.7 real-data use); when omitted (default) the behaviour is
    byte-identical to the frozen v0.6 baseline, where every panel column
    is a member of the frozen single-date snapshot.
    """

    momentum_lookback: int = 63
    momentum_quantile: float = 0.25
    quality_quantile: float = 0.5
    fundamentals: "pd.DataFrame | None" = None
    strategy_name: str = "momentum_quality"
    active_members: "pd.DataFrame | None" = None

    def __post_init__(self) -> None:
        if self.momentum_lookback < 2:
            raise ResearchInputError("momentum_lookback must be at least two")
        if not 0 < self.momentum_quantile <= 1:
            raise ResearchInputError("momentum_quantile must be in (0, 1]")
        if not 0 < self.quality_quantile <= 1:
            raise ResearchInputError("quality_quantile must be in (0, 1]")
        if self.active_members is not None and not isinstance(
            self.active_members, pd.DataFrame
        ):
            raise ResearchInputError("active_members must be a DataFrame or None")

    @property
    def name(self) -> str:
        """Return the stable strategy name."""
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Return serializable strategy parameters."""
        return {
            "momentum_lookback": self.momentum_lookback,
            "momentum_quantile": self.momentum_quantile,
            "quality_quantile": self.quality_quantile,
            "fundamentals_rows": len(self.fundamentals)
            if self.fundamentals is not None
            else 0,
        }

    def _active_mask(
        self, index: "pd.DatetimeIndex", columns: "pd.Index"
    ) -> pd.DataFrame:
        """Align the point-in-time membership mask to the data panel.

        Dates or symbols absent from the mask resolve to ``False``
        (never selected) so an incomplete mask can only be conservative,
        never look-ahead.
        """
        mask = self.active_members
        assert mask is not None  # guarded by callers
        # fillna BEFORE astype(bool): NaN.astype(bool) is True in
        # numpy/pandas, so missing membership must be replaced with False
        # first or absent dates/symbols would silently become eligible.
        return mask.reindex(index=index, columns=columns).fillna(False).astype(bool)

    def generate_signals(self, data: MarketData) -> Signal:
        """Generate momentum x quality long signals aligned with the data."""
        mask = (
            self._active_mask(data.close.index, data.close.columns)
            if self.active_members is not None
            else None
        )
        momentum = MomentumFactor(self.momentum_lookback).compute(data)
        if mask is not None:
            # Rank only within the point-in-time members of each date.
            momentum = momentum.where(mask)
        # Higher pct-rank == stronger trailing momentum. Select the top
        # ``momentum_quantile`` fraction of the universe.
        momentum_rank = momentum.rank(axis=1, pct=True, method="first")
        momentum_mask = momentum_rank.ge(1.0 - self.momentum_quantile).where(
            momentum_rank.notna()
        )
        if self.fundamentals is None or self.fundamentals.empty:
            raise ResearchInputError(
                "momentum_quality requires a fundamentals frame "
                "(date/symbol/roe/debt_to_equity)"
            )
        quality_factor = CompositeQualityFactor(
            [RoeQualityFactor(), DebtQualityFactor()]
        )
        quality_panel = quality_factor.compute(self.fundamentals)
        quality_panel = quality_panel.reindex(columns=data.close.columns)
        # Point-in-time alignment onto the research calendar: reindex onto
        # the union of the calendar and the fundamental availability dates,
        # forward-fill, then reindex back. Without the union step an
        # availability date that is not itself a trading day (quarter ends
        # on weekends or exchange holidays) would be dropped by the
        # reindex and its figures would never become effective. For
        # fundamentals already stamped on calendar dates (the v0.6
        # synthetic path) the union adds nothing and the result is
        # byte-identical.
        extended_index = data.close.index.union(pd.DatetimeIndex(quality_panel.index))
        daily_quality = (
            quality_panel.reindex(extended_index).ffill().reindex(data.close.index)
        )
        if mask is not None:
            daily_quality = daily_quality.where(mask)
        # Higher quality pct-rank == better composite score. Keep the top
        # ``quality_quantile`` fraction.
        quality_rank = daily_quality.rank(axis=1, pct=True, method="first")
        quality_mask = quality_rank.ge(1.0 - self.quality_quantile).where(
            quality_rank.notna()
        )
        selected = momentum_rank.where(momentum_mask & quality_mask, 0.0)
        selected = selected.fillna(0.0)
        return Signal(
            selected,
            metadata={
                "strategy": self.name,
                "factor": MomentumFactor(self.momentum_lookback).metadata.to_dict(),
                "quality": quality_factor.metadata.to_dict(),
                "momentum_quantile": self.momentum_quantile,
                "quality_quantile": self.quality_quantile,
                "active_members_mask": mask is not None,
            },
        )


@dataclass(frozen=True, slots=True)
class _QuantileRankStrategy(Strategy):
    """Base for cross-sectional quantile-ranked strategies.

    The membership mask (when supplied) is applied **before** ranking:
    only assets that are eligible on a date may rank on that date. Ranking
    first and masking afterwards is a look-ahead/survivorship bug this base
    class exists to prevent.
    """

    quantile: float = 0.25
    active_members: "pd.DataFrame | None" = None
    strategy_name: str = "quantile_rank"

    def __post_init__(self) -> None:
        if not 0 < self.quantile <= 1:
            raise ResearchInputError("quantile must be in (0, 1]")
        if self.active_members is not None and not isinstance(
            self.active_members, pd.DataFrame
        ):
            raise ResearchInputError("active_members must be a DataFrame or None")

    @property
    def name(self) -> str:
        """Return the stable strategy name."""
        return self.strategy_name

    def _mask(
        self, index: "pd.DatetimeIndex", columns: "pd.Index"
    ) -> "pd.DataFrame | None":
        if self.active_members is None:
            return None
        # fillna BEFORE astype(bool): NaN.astype(bool) is True, so missing
        # membership must become False first (mask-before-rank contract).
        return (
            self.active_members.reindex(index=index, columns=columns)
            .fillna(False)
            .astype(bool)
        )

    def _rank_select(self, values: pd.DataFrame, *, top: bool) -> pd.DataFrame:
        """Rank within eligible assets and select the chosen quantile."""
        mask = self._mask(values.index, values.columns)
        if mask is not None:
            # Mask BEFORE ranking — the point-in-time universe contract.
            values = values.where(mask)
        rank = values.rank(axis=1, pct=True, method="first")
        if top:
            selected = rank.ge(1.0 - self.quantile)
        else:
            selected = rank.le(self.quantile)
        return selected.where(rank.notna()).fillna(0.0).astype(float)


@dataclass(frozen=True, slots=True)
class CrossSectionalMomentumStrategy(_QuantileRankStrategy):
    """Cross-sectional momentum: top ``quantile`` of trailing returns."""

    lookback: int = 126
    strategy_name: str = "cross_sectional_momentum"

    def __post_init__(self) -> None:
        _QuantileRankStrategy.__post_init__(self)
        if self.lookback < 2:
            raise ResearchInputError("lookback must be at least two")

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "lookback": self.lookback,
            "quantile": self.quantile,
            "active_members": self.active_members is not None,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        values = MomentumFactor(self.lookback).compute(data)
        return Signal(
            self._rank_select(values, top=True),
            metadata={
                "strategy": self.name,
                "factor": MomentumFactor(self.lookback).metadata.to_dict(),
                "quantile": self.quantile,
            },
        )


@dataclass(frozen=True, slots=True)
class TrendFollowingStrategy(_QuantileRankStrategy):
    """Trend following: hold assets trading above their slow moving average.

    The quantile field is unused by design (the filter is binary); it is
    accepted only so every zoo family shares one constructor contract.
    """

    slow_window: int = 200
    method: str = "sma"
    strategy_name: str = "trend_following"

    def __post_init__(self) -> None:
        if self.slow_window < 20:
            raise ResearchInputError("slow_window must be at least 20")
        if self.method not in ("sma", "ema"):
            raise ResearchInputError("method must be sma or ema")

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "slow_window": self.slow_window,
            "method": self.method,
            "active_members": self.active_members is not None,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        factor = (
            SMAFactor(self.slow_window)
            if self.method == "sma"
            else EMAFactor(self.slow_window)
        )
        above = (data.close > factor.compute(data)).astype(float)
        mask = self._mask(data.close.index, data.close.columns)
        if mask is not None:
            above = above.where(mask, 0.0)
        return Signal(
            above,
            metadata={
                "strategy": self.name,
                "factor": factor.metadata.to_dict(),
                "method": self.method,
            },
        )


@dataclass(frozen=True, slots=True)
class LowVolatilityStrategy(_QuantileRankStrategy):
    """Low volatility: hold the ``quantile`` fraction of lowest realized vol."""

    window: int = 63
    strategy_name: str = "low_volatility"

    def __post_init__(self) -> None:
        _QuantileRankStrategy.__post_init__(self)
        if self.window < 5:
            raise ResearchInputError("window must be at least five")

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "window": self.window,
            "quantile": self.quantile,
            "active_members": self.active_members is not None,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        factor = RollingVolatilityFactor(self.window)
        values = factor.compute(data)
        # Lowest volatility ranks first: select the bottom quantile.
        return Signal(
            self._rank_select(values, top=False),
            metadata={
                "strategy": self.name,
                "factor": factor.metadata.to_dict(),
                "quantile": self.quantile,
            },
        )


@dataclass(frozen=True, slots=True)
class ReversalStrategy(_QuantileRankStrategy):
    """Short-term reversal: hold the most oversold ``quantile`` of assets."""

    window: int = 20
    strategy_name: str = "reversal"

    def __post_init__(self) -> None:
        _QuantileRankStrategy.__post_init__(self)
        if self.window < 5:
            raise ResearchInputError("window must be at least five")

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "window": self.window,
            "quantile": self.quantile,
            "active_members": self.active_members is not None,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        factor = ZScoreFactor(self.window)
        values = -factor.compute(data)
        # Most negative z-score (most oversold) ranks first.
        return Signal(
            self._rank_select(values, top=True),
            metadata={
                "strategy": self.name,
                "factor": factor.metadata.to_dict(),
                "quantile": self.quantile,
            },
        )


@dataclass(frozen=True, slots=True)
class QualityRankingStrategy(_QuantileRankStrategy):
    """Quality: hold the top ``quantile`` by composite fundamental quality.

    Requires a point-in-time fundamentals frame (date/symbol/roe/
    debt_to_equity). Quality observations are forward-filled from their
    availability dates — never from the future.
    """

    fundamentals: "pd.DataFrame | None" = None
    strategy_name: str = "quality"

    def __post_init__(self) -> None:
        _QuantileRankStrategy.__post_init__(self)
        if self.fundamentals is not None and not isinstance(
            self.fundamentals, pd.DataFrame
        ):
            raise ResearchInputError("fundamentals must be a DataFrame or None")

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "quantile": self.quantile,
            "fundamentals_rows": len(self.fundamentals)
            if self.fundamentals is not None
            else 0,
            "active_members": self.active_members is not None,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        if self.fundamentals is None or self.fundamentals.empty:
            raise ResearchInputError(
                "quality ranking requires a fundamentals frame "
                "(date/symbol/roe/debt_to_equity)"
            )
        quality_factor = CompositeQualityFactor(
            [RoeQualityFactor(), DebtQualityFactor()]
        )
        quality_panel = quality_factor.compute(self.fundamentals)
        quality_panel = quality_panel.reindex(columns=data.close.columns)
        extended_index = data.close.index.union(pd.DatetimeIndex(quality_panel.index))
        daily_quality = (
            quality_panel.reindex(extended_index).ffill().reindex(data.close.index)
        )
        return Signal(
            self._rank_select(daily_quality, top=True),
            metadata={
                "strategy": self.name,
                "quality": quality_factor.metadata.to_dict(),
                "quantile": self.quantile,
            },
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
        if normalized == "cross_sectional_momentum":
            return CrossSectionalMomentumStrategy(
                lookback=int(values.get("lookback", 126)),
                quantile=float(values.get("quantile", 0.25)),
            )
        if normalized == "trend_following":
            return TrendFollowingStrategy(
                slow_window=int(values.get("slow_window", 200)),
                method=str(values.get("method", "sma")),
            )
        if normalized == "low_volatility":
            return LowVolatilityStrategy(
                window=int(values.get("window", 63)),
                quantile=float(values.get("quantile", 0.25)),
            )
        if normalized in {"reversal", "mean_reversion_quantile"}:
            return ReversalStrategy(
                window=int(values.get("window", 20)),
                quantile=float(values.get("quantile", 0.25)),
            )
        if normalized == "quality":
            return QualityRankingStrategy(
                quantile=float(values.get("quantile", 0.5)),
                fundamentals=values.get("fundamentals"),
            )
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(f"invalid parameters for strategy {name!r}") from exc
    raise ResearchInputError(f"unsupported strategy: {name}")
