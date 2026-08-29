"""Transparent, deterministic target-weight portfolio simulator for discovery.

This is a *discovery* engine, deliberately simple and fast. It rebalances at a
configured frequency, charges proportional one-way cost on turnover, and
supports long-only, long-short, and gross-scaled allocations.

Validation gaps (reported, not hidden):
* It assumes fills at next-day close prices (no intraday, no bid/ask).
* It does not model lot sizes, market impact beyond a fixed bps, or borrow
  constraints.
* Position sizing is per-target-weight; leverage is explicit in the config.
* Corporate actions are assumed already reflected in adjusted prices.

The production ``backtest`` engine remains the place for cost-level and
execution-level realism.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DiscoveryConfig:
    rebalance_frequency: str = "M"
    one_way_cost_bps: float = 12.0
    periods_per_year: int = 252


@dataclass(frozen=True)
class DiscoveryResult:
    returns: pd.Series
    equity: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    trades: pd.DataFrame
    metrics: Mapping[str, float]
    metadata: Mapping[str, object]


def _rebalance_mask(index: pd.DatetimeIndex, frequency: str) -> pd.Series:
    periods = index.to_period(frequency)
    mask = pd.Series(~periods.duplicated(keep="last"), index=index)
    mask.iloc[0] = True
    return mask


def _weights_to_portfolio_returns(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    rebalance: pd.Series,
    cost_rate: float,
) -> tuple[pd.Series, pd.DataFrame]:
    asset_returns = prices.pct_change().fillna(0.0)
    weights = target_weights.where(rebalance, np.nan).ffill().fillna(0.0)
    # Effective exposure at time t comes from the weights decided at t-1,
    # preventing same-bar look-ahead.
    effective = weights.shift(1).fillna(0.0)
    portfolio_returns = (asset_returns * effective).sum(axis=1)
    turnover = (weights - effective).abs().sum(axis=1).where(rebalance, 0.0)
    cost = turnover * cost_rate
    net_returns = portfolio_returns - cost
    return net_returns, weights


def _metrics(net_returns: pd.Series, periods_per_year: int) -> dict[str, float]:
    returns = net_returns.dropna()
    if returns.empty:
        return {}
    equity = (1.0 + returns).cumprod()
    total = float(equity.iloc[-1] - 1.0)
    n = len(returns)
    ann = float((1.0 + total) ** (periods_per_year / n) - 1.0)
    vol = float(returns.std(ddof=1) * np.sqrt(periods_per_year))
    sharpe = float(ann / vol) if vol > 0 else 0.0
    downside = returns[returns < 0]
    dvol = float(downside.std(ddof=1) * np.sqrt(periods_per_year)) if len(downside) > 1 else 0.0
    sortino = float(ann / dvol) if dvol > 0 else 0.0
    drawdown = equity / equity.cummax() - 1.0
    max_dd = float(drawdown.min())
    active = returns[returns != 0]
    win = float((active > 0).mean()) if len(active) else 0.0
    return {
        "total_return": total,
        "cagr": ann,
        "volatility": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "win_rate": win,
        "periods": float(n),
    }


def simulate(
    prices: pd.DataFrame,
    target_weights: pd.DataFrame,
    *,
    config: DiscoveryConfig | None = None,
    metadata: Mapping[str, object] | None = None,
    strategy_name: str = "strategy",
) -> DiscoveryResult:
    """Run a target-weight simulation.

    ``prices`` and ``target_weights`` must share the same index/columns, be
    finite, and be sorted by the index.
    """
    config = config or DiscoveryConfig()
    if prices.index.tolist() != target_weights.index.tolist() or prices.columns.tolist() != target_weights.columns.tolist():
        raise ValueError("prices and target_weights must align exactly")
    target = target_weights.copy().apply(pd.to_numeric, errors="coerce").fillna(0.0)
    rebalance = _rebalance_mask(prices.index, config.rebalance_frequency)
    cost_rate = config.one_way_cost_bps / 10_000.0
    net_returns, weights = _weights_to_portfolio_returns(
        prices, target, rebalance, cost_rate
    )
    equity = (1.0 + net_returns).cumprod()
    turnover = (weights - weights.shift(1).fillna(0.0)).abs().sum(axis=1).where(rebalance, 0.0)
    trades = pd.DataFrame(
        {
            "turnover": turnover,
            "cost": turnover * cost_rate,
            "return": net_returns,
            "equity": equity,
        }
    )
    metrics = _metrics(net_returns, config.periods_per_year)
    trade_rows = []
    row_positions = {ts: i for i, ts in enumerate(weights.index)}
    for idx in trades.index[trades["turnover"] > 0]:
        pos = row_positions[idx]
        prev_pos = max(0, pos - 1)
        change = (weights.iloc[pos] - weights.iloc[prev_pos]).abs()
        top = change.sort_values(ascending=False).head(10)
        for symbol, delta in top.items():
            if delta > 0:
                trade_rows.append({"date": idx, "symbol": symbol, "weight_change": delta})
    trades_legs = (
        pd.DataFrame(trade_rows)
        if trade_rows
        else pd.DataFrame(columns=["date", "symbol", "weight_change"])
    )
    return DiscoveryResult(
        returns=net_returns,
        equity=equity,
        weights=weights,
        turnover=turnover,
        trades=trades_legs,
        metrics=metrics,
        metadata=dict(metadata or {"strategy": strategy_name, "backend": "discovery"}),
    )
