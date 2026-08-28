"""Reusable factor-backed strategy signal generators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from models.quality import (
    CompositeQualityFactor,
    DebtQualityFactor,
    PeRatioQualityFactor,
    RoeQualityFactor,
)

from .contracts import Factor, MarketData, ResearchInputError, Signal, Strategy
from .factors import (
    ATRFactor,
    BollingerDeviationFactor,
    MomentumFactor,
    MovingAverageCrossoverFactor,
    RSIFactor,
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
class CrossSectionalMomentumStrategy(Strategy):
    """S01: Cross-sectional momentum equity strategy.

    Ranks liquid universe cross-sectionally by trailing return over lookback periods
    (e.g., 3M / 63d, 6M / 126d, 12M / 252d). Holds the top momentum quantile or top N
    winners and avoids the bottom losers.
    """

    lookback: int = 63
    quantile: float = 0.20
    multi_horizon: bool = False
    strategy_name: str = "cross_sectional_momentum"
    active_members: "pd.DataFrame | None" = None

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ResearchInputError("lookback must be at least two")
        if not 0 < self.quantile <= 1:
            raise ResearchInputError("quantile must be in (0, 1]")
        if self.active_members is not None and not isinstance(
            self.active_members, pd.DataFrame
        ):
            raise ResearchInputError("active_members must be a DataFrame or None")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "lookback": self.lookback,
            "quantile": self.quantile,
            "multi_horizon": self.multi_horizon,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        if self.multi_horizon:
            m1 = data.close.pct_change(21).rank(axis=1, pct=True, method="first")
            m3 = data.close.pct_change(63).rank(axis=1, pct=True, method="first")
            m6 = data.close.pct_change(126).rank(axis=1, pct=True, method="first")
            m12 = data.close.pct_change(252).rank(axis=1, pct=True, method="first")
            rank = (
                m1.fillna(0.5) + m3.fillna(0.5) + m6.fillna(0.5) + m12.fillna(0.5)
            ) / 4.0
        else:
            momentum = data.close.pct_change(self.lookback)
            rank = momentum.rank(axis=1, pct=True, method="first")

        if self.active_members is not None:
            mask = (
                self.active_members.reindex(
                    index=data.close.index, columns=data.close.columns
                )
                .astype(bool)
                .fillna(False)
            )
            rank = rank.where(mask)

        threshold = 1.0 - self.quantile
        signals = rank.where(rank >= threshold, 0.0).fillna(0.0)
        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "lookback": self.lookback,
                "quantile": self.quantile,
                "multi_horizon": self.multi_horizon,
            },
        )


@dataclass(frozen=True, slots=True)
class DonchianTrendStrategy(Strategy):
    """S02: Donchian channel trend-following breakout strategy.

    Classic mechanical trend system (Turtle / Managed Futures).
    Buys when price exceeds the upper channel (highest high of entry_window, shifted 1 bar).
    Exits when price falls below the lower channel (lowest low of exit_window, shifted 1 bar).
    Zero look-ahead, state-preserving trend signals.
    """

    entry_window: int = 20
    exit_window: int = 10
    volatility_weighted: bool = False
    strategy_name: str = "donchian_trend"

    def __post_init__(self) -> None:
        if self.entry_window < 2:
            raise ResearchInputError("entry_window must be at least two")
        if self.exit_window < 2:
            raise ResearchInputError("exit_window must be at least two")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "entry_window": self.entry_window,
            "exit_window": self.exit_window,
            "volatility_weighted": self.volatility_weighted,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        high_source = data.high if data.high is not None else data.close
        low_source = data.low if data.low is not None else data.close

        upper = (
            high_source.shift(1)
            .rolling(self.entry_window, min_periods=self.entry_window)
            .max()
        )
        lower = (
            low_source.shift(1)
            .rolling(self.exit_window, min_periods=self.exit_window)
            .min()
        )

        entries = (data.close > upper).astype(float)
        exits = (data.close < lower).astype(float)

        raw_state = pd.DataFrame(
            index=data.close.index, columns=data.close.columns, dtype=float
        )
        raw_state = raw_state.where(entries != 1.0, 1.0)
        raw_state = raw_state.where(exits != 1.0, 0.0)
        signals = raw_state.ffill().fillna(0.0)

        if self.volatility_weighted:
            vol = data.close.pct_change().rolling(20, min_periods=20).std()
            inv_vol = (1.0 / vol.replace(0, pd.NA)).fillna(1.0)
            signals = signals * inv_vol

        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "entry_window": self.entry_window,
                "exit_window": self.exit_window,
            },
        )


@dataclass(frozen=True, slots=True)
class PairsTradingStrategy(Strategy):
    """S03: Pairs trading / statistical arbitrage strategy.

    Measures mean-reverting spread z-score between cointegrated or correlated assets.
    Enters long on underperforming asset when spread diverges beyond entry_zscore;
    exits when spread normalizes back to exit_zscore or triggers stop_zscore.
    """

    window: int = 60
    entry_zscore: float = 2.0
    exit_zscore: float = 0.5
    stop_zscore: float = 3.5
    symbol_a: str | None = None
    symbol_b: str | None = None
    strategy_name: str = "pairs_trading"

    def __post_init__(self) -> None:
        if self.window < 5:
            raise ResearchInputError("window must be at least five")
        if self.entry_zscore <= 0 or self.exit_zscore < 0:
            raise ResearchInputError("z-score thresholds must be non-negative")
        if self.entry_zscore <= self.exit_zscore:
            raise ResearchInputError("entry_zscore must exceed exit_zscore")
        if self.stop_zscore <= self.entry_zscore:
            raise ResearchInputError("stop_zscore must exceed entry_zscore")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "window": self.window,
            "entry_zscore": self.entry_zscore,
            "exit_zscore": self.exit_zscore,
            "stop_zscore": self.stop_zscore,
            "symbol_a": self.symbol_a,
            "symbol_b": self.symbol_b,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        cols = list(data.close.columns)
        signals = pd.DataFrame(0.0, index=data.close.index, columns=data.close.columns)

        sym_a = (
            self.symbol_a
            if self.symbol_a in cols
            else (cols[0] if len(cols) >= 1 else None)
        )
        sym_b = (
            self.symbol_b
            if self.symbol_b in cols
            else (cols[1] if len(cols) >= 2 else None)
        )

        if sym_a and sym_b and sym_a != sym_b:
            p_a = data.close[sym_a]
            p_b = data.close[sym_b]
            spread = p_a / p_b.replace(0, pd.NA)
            rolling_mean = (
                spread.shift(1).rolling(self.window, min_periods=self.window).mean()
            )
            rolling_std = (
                spread.shift(1).rolling(self.window, min_periods=self.window).std()
            )
            z_score = (spread - rolling_mean) / rolling_std.replace(0, pd.NA)

            long_a = (z_score < -self.entry_zscore) & (z_score > -self.stop_zscore)
            exit_a = (z_score >= -self.exit_zscore) | (z_score <= -self.stop_zscore)

            long_b = (z_score > self.entry_zscore) & (z_score < self.stop_zscore)
            exit_b = (z_score <= self.exit_zscore) | (z_score >= self.stop_zscore)

            state_a = pd.Series(index=data.close.index, dtype=float)
            state_a = state_a.where(~long_a, 1.0)
            state_a = state_a.where(~exit_a, 0.0)
            signals[sym_a] = state_a.ffill().fillna(0.0)

            state_b = pd.Series(index=data.close.index, dtype=float)
            state_b = state_b.where(~long_b, 1.0)
            state_b = state_b.where(~exit_b, 0.0)
            signals[sym_b] = state_b.ffill().fillna(0.0)
        else:
            rel_perf = data.close.pct_change(self.window)
            median_perf = rel_perf.median(axis=1)
            spread = rel_perf.sub(median_perf, axis=0)
            std = rel_perf.std(axis=1).replace(0, pd.NA)
            z_score = spread.div(std, axis=0)
            signals = (z_score < -self.entry_zscore).astype(float).fillna(0.0)

        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "window": self.window,
                "entry_zscore": self.entry_zscore,
            },
        )


@dataclass(frozen=True, slots=True)
class RsiMeanReversionStrategy(Strategy):
    """S04: RSI mean-reversion strategy.

    Buys when RSI drops below oversold threshold (e.g. 30 or 10 for RSI-2);
    exits when RSI rises above overbought threshold (e.g. 70 or 50).
    Optional trend filter (e.g. 200 SMA) prevents dip-buying during severe bear regimes.
    """

    rsi_window: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    trend_filter_window: int | None = None
    strategy_name: str = "rsi_mean_reversion"

    def __post_init__(self) -> None:
        if self.rsi_window < 2:
            raise ResearchInputError("rsi_window must be at least two")
        if not 0 < self.oversold < self.overbought < 100:
            raise ResearchInputError(
                "oversold and overbought must satisfy 0 < oversold < overbought < 100"
            )
        if self.trend_filter_window is not None and self.trend_filter_window < 2:
            raise ResearchInputError("trend_filter_window must be at least two")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "rsi_window": self.rsi_window,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "trend_filter_window": self.trend_filter_window,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        rsi = RSIFactor(self.rsi_window).compute(data)
        entries = rsi < self.oversold
        exits = rsi > self.overbought

        if self.trend_filter_window is not None:
            sma = data.close.rolling(
                self.trend_filter_window, min_periods=self.trend_filter_window
            ).mean()
            entries = entries & (data.close > sma)

        raw_state = pd.DataFrame(
            index=data.close.index, columns=data.close.columns, dtype=float
        )
        raw_state = raw_state.where(~entries, 1.0)
        raw_state = raw_state.where(~exits, 0.0)
        signals = raw_state.ffill().fillna(0.0)

        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "rsi_window": self.rsi_window,
                "oversold": self.oversold,
                "overbought": self.overbought,
            },
        )


@dataclass(frozen=True, slots=True)
class OrbStrategy(Strategy):
    """S05: Opening Range Breakout (ORB) strategy.

    Simulates breakout beyond a volatility-adjusted opening range.
    Triggers long signal when price exceeds the opening barrier (ATR hurdle).
    """

    range_factor: float = 0.5
    atr_window: int = 14
    strategy_name: str = "orb"

    def __post_init__(self) -> None:
        if self.range_factor <= 0:
            raise ResearchInputError("range_factor must be positive")
        if self.atr_window < 2:
            raise ResearchInputError("atr_window must be at least two")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "range_factor": self.range_factor,
            "atr_window": self.atr_window,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        if data.high is not None and data.low is not None:
            atr = ATRFactor(self.atr_window).compute(data)
        else:
            atr = (
                data.close.pct_change()
                .rolling(self.atr_window, min_periods=self.atr_window)
                .std()
                * data.close
            )
        hurdle = self.range_factor * atr.shift(1)
        prev_close = data.close.shift(1)
        breakout = data.close > (prev_close + hurdle)
        signals = breakout.astype(float).fillna(0.0)
        return Signal(
            signals,
            metadata={"strategy": self.name, "range_factor": self.range_factor},
        )


@dataclass(frozen=True, slots=True)
class GapFadeStrategy(Strategy):
    """S06: Overnight gap fade mean-reversion strategy.

    Identifies moderate overnight gap-downs and buys for mean-reversion bounce,
    avoiding severe crashes beyond max_gap_pct.
    """

    min_gap_pct: float = -0.005
    max_gap_pct: float = -0.035
    strategy_name: str = "gap_fade"

    def __post_init__(self) -> None:
        if self.min_gap_pct <= self.max_gap_pct:
            raise ResearchInputError("min_gap_pct must be greater than max_gap_pct")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "min_gap_pct": self.min_gap_pct,
            "max_gap_pct": self.max_gap_pct,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        gap = data.close.pct_change(1)
        favorable_gap = (gap <= self.min_gap_pct) & (gap >= self.max_gap_pct)
        signals = favorable_gap.astype(float).fillna(0.0)
        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "min_gap_pct": self.min_gap_pct,
                "max_gap_pct": self.max_gap_pct,
            },
        )


@dataclass(frozen=True, slots=True)
class LowVolatilityStrategy(Strategy):
    """S07: Low-volatility factor anomaly strategy.

    Ranks universe cross-sectionally by trailing realized volatility and selects
    the top quantile of lowest-volatility equities.
    """

    vol_window: int = 63
    quantile: float = 0.25
    strategy_name: str = "low_volatility"

    def __post_init__(self) -> None:
        if self.vol_window < 2:
            raise ResearchInputError("vol_window must be at least two")
        if not 0 < self.quantile <= 1:
            raise ResearchInputError("quantile must be in (0, 1]")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "vol_window": self.vol_window,
            "quantile": self.quantile,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        vol = (
            data.close.pct_change()
            .rolling(self.vol_window, min_periods=self.vol_window)
            .std()
        )
        inv_vol = 1.0 / vol.replace(0, pd.NA)
        rank = inv_vol.rank(axis=1, pct=True, method="first")
        signals = rank.where(rank >= (1.0 - self.quantile), 0.0).fillna(0.0)
        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "vol_window": self.vol_window,
                "quantile": self.quantile,
            },
        )


@dataclass(frozen=True, slots=True)
class ValueQualityStrategy(Strategy):
    """S08: Value and fundamental quality factor strategy.

    Ranks assets on fundamental quality (ROE, low Debt/Equity) and value metrics
    (low P/E or value proxies).
    """

    quality_quantile: float = 0.5
    value_quantile: float = 0.5
    fundamentals: "pd.DataFrame | None" = None
    strategy_name: str = "value_quality"

    def __post_init__(self) -> None:
        if not 0 < self.quality_quantile <= 1:
            raise ResearchInputError("quality_quantile must be in (0, 1]")
        if not 0 < self.value_quantile <= 1:
            raise ResearchInputError("value_quantile must be in (0, 1]")

    @property
    def name(self) -> str:
        return self.strategy_name

    @property
    def parameters(self) -> Mapping[str, Any]:
        return {
            "quality_quantile": self.quality_quantile,
            "value_quantile": self.value_quantile,
            "fundamentals_rows": len(self.fundamentals)
            if self.fundamentals is not None
            else 0,
        }

    def generate_signals(self, data: MarketData) -> Signal:
        if self.fundamentals is not None and not self.fundamentals.empty:
            factors = [RoeQualityFactor(), DebtQualityFactor()]
            if "pe_ratio" in self.fundamentals.columns:
                factors.append(PeRatioQualityFactor())
            quality_factor = CompositeQualityFactor(factors)
            quality_panel = quality_factor.compute(self.fundamentals)
            extended_index = data.close.index.union(
                pd.DatetimeIndex(quality_panel.index)
            )
            daily_quality = (
                quality_panel.reindex(columns=data.close.columns)
                .reindex(extended_index)
                .ffill()
                .reindex(data.close.index)
            )
            q_rank = daily_quality.rank(axis=1, pct=True, method="first")
        else:
            stability = 1.0 / data.close.pct_change().rolling(
                63, min_periods=63
            ).std().replace(0, pd.NA)
            q_rank = stability.rank(axis=1, pct=True, method="first")

        sma_200 = data.close.rolling(200, min_periods=50).mean()
        value_ratio = 1.0 / (data.close / sma_200.replace(0, pd.NA)).replace(0, pd.NA)
        v_rank = value_ratio.rank(axis=1, pct=True, method="first")

        mask = (q_rank >= (1.0 - self.quality_quantile)) & (
            v_rank >= (1.0 - self.value_quantile)
        )
        signals = ((q_rank + v_rank) / 2.0).where(mask, 0.0).fillna(0.0)

        return Signal(
            signals,
            metadata={
                "strategy": self.name,
                "quality_quantile": self.quality_quantile,
                "value_quantile": self.value_quantile,
            },
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
    """Cross-sectional momentum + quality screen (the baseline strategy)."""

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
        mask = self.active_members
        assert mask is not None
        return mask.reindex(index=index, columns=columns).astype(bool).fillna(False)

    def generate_signals(self, data: MarketData) -> Signal:
        mask = (
            self._active_mask(data.close.index, data.close.columns)
            if self.active_members is not None
            else None
        )
        momentum = MomentumFactor(self.momentum_lookback).compute(data)
        if mask is not None:
            momentum = momentum.where(mask)
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
        extended_index = data.close.index.union(pd.DatetimeIndex(quality_panel.index))
        daily_quality = (
            quality_panel.reindex(extended_index).ffill().reindex(data.close.index)
        )
        if mask is not None:
            daily_quality = daily_quality.where(mask)
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


def strategy_from_name(
    name: str,
    parameters: Mapping[str, Any] | None = None,
) -> Strategy:
    """Construct a supported research strategy from a configuration name."""
    normalized = name.strip().lower().replace("-", "_")
    values = dict(parameters or {})
    try:
        if normalized in {"momentum", "trailing_momentum"}:
            return MomentumStrategy(
                lookback=int(values.get("lookback", 63)),
                threshold=float(values.get("threshold", 0.0)),
            )
        if normalized in {
            "cross_sectional_momentum",
            "xs_momentum",
            "s01",
            "s01_cross_sectional_momentum",
        }:
            return CrossSectionalMomentumStrategy(
                lookback=int(values.get("lookback", 63)),
                quantile=float(values.get("quantile", 0.20)),
                multi_horizon=bool(values.get("multi_horizon", False)),
            )
        if normalized in {
            "donchian",
            "donchian_trend",
            "turtle",
            "trend_following",
            "s02",
            "s02_donchian_trend",
        }:
            return DonchianTrendStrategy(
                entry_window=int(values.get("entry_window", 20)),
                exit_window=int(values.get("exit_window", 10)),
                volatility_weighted=bool(values.get("volatility_weighted", False)),
            )
        if normalized in {
            "pairs",
            "pairs_trading",
            "stat_arb",
            "s03",
            "s03_pairs_trading",
        }:
            return PairsTradingStrategy(
                window=int(values.get("window", 60)),
                entry_zscore=float(values.get("entry_zscore", 2.0)),
                exit_zscore=float(values.get("exit_zscore", 0.5)),
                stop_zscore=float(values.get("stop_zscore", 3.5)),
                symbol_a=str(values["symbol_a"]) if values.get("symbol_a") else None,
                symbol_b=str(values["symbol_b"]) if values.get("symbol_b") else None,
            )
        if normalized in {
            "rsi",
            "rsi_mean_reversion",
            "s04",
            "s04_rsi_mean_reversion",
        }:
            return RsiMeanReversionStrategy(
                rsi_window=int(values.get("rsi_window", 14)),
                oversold=float(values.get("oversold", 30.0)),
                overbought=float(values.get("overbought", 70.0)),
                trend_filter_window=int(values["trend_filter_window"])
                if values.get("trend_filter_window")
                else None,
            )
        if normalized in {
            "orb",
            "opening_range_breakout",
            "s05",
            "s05_orb",
        }:
            return OrbStrategy(
                range_factor=float(values.get("range_factor", 0.5)),
                atr_window=int(values.get("atr_window", 14)),
            )
        if normalized in {
            "gap_fade",
            "gap_reversion",
            "s06",
            "s06_gap_fade",
        }:
            return GapFadeStrategy(
                min_gap_pct=float(values.get("min_gap_pct", -0.005)),
                max_gap_pct=float(values.get("max_gap_pct", -0.035)),
            )
        if normalized in {
            "low_volatility",
            "low_vol",
            "min_vol",
            "s07",
            "s07_low_volatility",
        }:
            return LowVolatilityStrategy(
                vol_window=int(values.get("vol_window", 63)),
                quantile=float(values.get("quantile", 0.25)),
            )
        if normalized in {
            "value_quality",
            "quality_value",
            "s08",
            "s08_value_quality",
        }:
            return ValueQualityStrategy(
                quality_quantile=float(values.get("quality_quantile", 0.5)),
                value_quantile=float(values.get("value_quantile", 0.5)),
                fundamentals=values.get("fundamentals"),
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
        if normalized in {"momentum_quality", "momentum_quality_baseline"}:
            return MomentumQualityStrategy(
                momentum_lookback=int(values.get("momentum_lookback", 63)),
                momentum_quantile=float(values.get("momentum_quantile", 0.25)),
                quality_quantile=float(values.get("quality_quantile", 0.5)),
                fundamentals=values.get("fundamentals"),
            )
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(f"invalid parameters for strategy {name!r}") from exc
    raise ResearchInputError(f"unsupported strategy: {name}")
