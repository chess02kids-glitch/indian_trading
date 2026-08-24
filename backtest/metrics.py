"""Deterministic performance metrics for research backtests."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite, sqrt

import pandas as pd

from research.contracts import ResearchInputError


def _returns_series(returns: pd.Series) -> pd.Series:
    if not isinstance(returns, pd.Series) or returns.empty:
        raise ResearchInputError("returns must be a non-empty pandas Series")
    if not isinstance(returns.index, pd.DatetimeIndex):
        raise ResearchInputError("returns must use a DatetimeIndex")
    if not returns.index.is_unique or not returns.index.is_monotonic_increasing:
        raise ResearchInputError("returns index must be sorted and unique")
    values = pd.to_numeric(returns, errors="coerce")
    if values.isna().any() or not ((values + 1) >= 0).all():
        raise ResearchInputError(
            "returns must be numeric and no less than -100 percent"
        )
    return values.astype(float)


def equity_curve(returns: pd.Series, initial_value: float = 1.0) -> pd.Series:
    """Compound periodic returns from a positive initial value."""
    if initial_value <= 0:
        raise ResearchInputError("initial_value must be positive")
    values = _returns_series(returns)
    return initial_value * (1.0 + values).cumprod()


def drawdown(equity: pd.Series) -> pd.Series:
    """Return percentage drawdown from each running equity peak."""
    if not isinstance(equity, pd.Series) or equity.empty:
        raise ResearchInputError("equity must be a non-empty pandas Series")
    if (equity < 0).any():
        raise ResearchInputError(
            "equity must remain non-negative for drawdown calculation"
        )
    peaks = equity.cummax()
    ratio = equity.div(peaks.where(peaks != 0))
    return ratio.fillna(0.0).sub(1.0)


def rolling_sharpe(
    returns: pd.Series,
    window: int = 63,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> pd.Series:
    """Calculate trailing annualized Sharpe ratios."""
    if window < 2 or periods_per_year < 1:
        raise ResearchInputError(
            "window must be at least two and periods_per_year positive"
        )
    values = _returns_series(returns)
    excess = values - risk_free_rate / periods_per_year
    volatility = excess.rolling(window, min_periods=window).std()
    return (
        excess.rolling(window, min_periods=window).mean()
        / volatility
        * sqrt(periods_per_year)
    )


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Standardized performance summary for a return series."""

    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe: float
    max_drawdown: float
    calmar: float
    turnover: float
    observations: int
    sortino: float = 0.0
    win_rate: float | None = None
    trade_count: int = 0
    cost_drag: float = 0.0

    def to_dict(self) -> dict[str, float | int | None]:
        """Return metrics in a report-friendly mapping."""
        return {
            "total_return": self.total_return,
            "annualized_return": self.annualized_return,
            "annualized_volatility": self.annualized_volatility,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "max_drawdown": self.max_drawdown,
            "calmar": self.calmar,
            "turnover": self.turnover,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "cost_drag": self.cost_drag,
            "observations": self.observations,
        }


def compute_performance_metrics(
    returns: pd.Series,
    turnover: pd.Series | None = None,
    *,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
    initial_value: float = 1.0,
    total_cost: float | None = None,
    trade_count: int | None = None,
) -> PerformanceMetrics:
    """Compute return, risk, drawdown, Sharpe/Sortino, and turnover metrics."""
    if periods_per_year < 1:
        raise ResearchInputError("periods_per_year must be positive")
    values = _returns_series(returns)
    curve = equity_curve(values, initial_value)
    excess = values - risk_free_rate / periods_per_year
    standard_deviation = float(excess.std(ddof=1))
    volatility = standard_deviation * sqrt(periods_per_year)
    sharpe = (
        float(excess.mean() / standard_deviation * sqrt(periods_per_year))
        if standard_deviation > 0
        else 0.0
    )
    downside = excess[excess < 0]
    downside_deviation = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = (
        float(excess.mean() / downside_deviation * sqrt(periods_per_year))
        if downside_deviation > 0
        else 0.0
    )
    win_rate = float((values > 0).mean()) if len(values) else None
    total = float(curve.iloc[-1] / initial_value - 1.0)
    annualized = float((1.0 + total) ** (periods_per_year / len(values)) - 1.0)
    max_drawdown = float(drawdown(curve).min())
    calmar = annualized / abs(max_drawdown) if max_drawdown < 0 else 0.0
    total_turnover = 0.0
    cost_drag = 0.0
    if turnover is not None:
        if not isinstance(turnover, pd.Series) or not turnover.index.equals(
            values.index
        ):
            raise ResearchInputError("turnover must align with returns")
        numeric_turnover = pd.to_numeric(turnover, errors="coerce")
        if (
            numeric_turnover.isna().any()
            or (numeric_turnover < 0).any()
            or not numeric_turnover.map(isfinite).all()
        ):
            raise ResearchInputError("turnover must be non-negative and finite")
        total_turnover = float(numeric_turnover.sum())
    if total_cost is not None:
        if not isfinite(total_cost) or total_cost < 0:
            raise ResearchInputError("total_cost must be finite and non-negative")
        cost_drag = float(total_cost) / initial_value
    if trade_count is not None and trade_count < 0:
        raise ResearchInputError("trade_count must be non-negative")
    return PerformanceMetrics(
        total_return=total,
        annualized_return=annualized,
        annualized_volatility=volatility if pd.notna(volatility) else 0.0,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        calmar=float(calmar),
        turnover=total_turnover,
        win_rate=win_rate,
        trade_count=int(trade_count) if trade_count is not None else 0,
        cost_drag=cost_drag,
        observations=len(values),
    )
