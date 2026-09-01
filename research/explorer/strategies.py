"""Breadth-first, hypothesis-driven signal generators for discovery.

Each function maps a MarketData-like dictionary of panels to a *target-weight*
DataFrame, plus a serializable description. Discovery is intentionally broad;
a weak result here is not a rejection until the stricter engine confirms it.

Signal timing convention: a weight computed from data up to close ``t`` is
simulated at ``t+1`` (the simulator shifts weights once), so there is no
same-bar look-ahead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np
import pandas as pd

from .sim import DiscoveryConfig, simulate


@dataclass(frozen=True)
class StrategySpec:
    name: str
    family: str
    hypothesis: str
    parameters: Mapping[str, object]
    generate: Callable[[Mapping[str, pd.DataFrame]], pd.DataFrame]


def _ffill_zero(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.ffill().fillna(0.0)


def _rank_pct(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rank(axis=1, pct=True, method="first")


def _cross_sectional_weights(values: pd.DataFrame, top_share: float) -> pd.DataFrame:
    top = _rank_pct(values).ge(1.0 - top_share)
    denom = top.sum(axis=1)
    out = top.astype(float).div(denom, axis=0)
    # rows with no picks get 0
    return out.fillna(0.0)


def _truncate_bounds(prices: pd.DataFrame, min_h: int, max_h: int) -> pd.DataFrame:
    return prices.loc[min_h:max_h]


def _ema(frame: pd.DataFrame, span: int) -> pd.DataFrame:
    return frame.ewm(span=span, adjust=False).mean()


def _momentum(close: pd.DataFrame, lookback: int) -> pd.DataFrame:
    return close / close.shift(lookback) - 1.0


def _rolling_vol(close: pd.DataFrame, window: int) -> pd.DataFrame:
    return close.pct_change().rolling(window).std()


def _low_vol_weights(close: pd.DataFrame, window: int, top_share: float) -> pd.DataFrame:
    vol = _rolling_vol(close, window)
    return _cross_sectional_weights(vol, top_share)


def _time_series_momentum_weights(
    close: pd.DataFrame,
    lookback: int,
    top_share: float,
    vol_target: float | float | None,
) -> pd.DataFrame:
    mom = _momentum(close, lookback)
    sign = np.sign(mom).clip(lower=0.0)
    ranked = _rank_pct(mom)
    top = ranked.ge(1.0 - top_share) & sign.gt(0)
    denom = top.sum(axis=1)
    raw = top.astype(float).div(denom, axis=0).fillna(0.0)
    if vol_target is not None:
        raw_vol = (raw.abs() * 1.0).sum(axis=1)
        scale = vol_target / raw_vol.replace(0.0, np.nan)
        raw = raw.mul(scale, axis=0).clip(upper=1.0)
    return raw


def _ma_trend_weights(
    close: pd.DataFrame,
    fast: int,
    slow: int,
    top_share: float,
) -> pd.DataFrame:
    trend = _ema(close, fast) > _ema(close, slow)
    trend = trend.astype(float).replace(0.0, np.nan)
    # only pick names in positive trend, but scored by relative trend strength
    rel = (_ema(close, fast) / _ema(close, slow) - 1.0)
    ranked = _rank_pct(rel.where(trend.astype(bool)))
    top = ranked.ge(1.0 - top_share)
    denom = top.sum(axis=1)
    return top.astype(float).div(denom, axis=0).fillna(0.0)


def _rsi(close: pd.DataFrame, window: int) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _rsi2_weights(
    close: pd.DataFrame, window: int, threshold: float, top_share: float
) -> pd.DataFrame:
    rsi = _rsi(close, window)
    signal = rsi < threshold
    # Long the lowest RSI names.
    ranked = _rank_pct(rsi.where(signal))
    top = ranked.ge(1.0 - top_share)
    denom = top.sum(axis=1)
    return top.astype(float).div(denom, axis=0).fillna(0.0)


def _bollinger_weights(
    close: pd.DataFrame, window: int, num_std: float, top_share: float
) -> pd.DataFrame:
    sma = close.rolling(window).mean()
    std = close.rolling(window).std()
    z = (close - sma) / std.replace(0.0, np.nan)
    signal = z < -num_std
    ranked = _rank_pct(z.where(signal))
    top = ranked.ge(1.0 - top_share)
    denom = top.sum(axis=1)
    return top.astype(float).div(denom, axis=0).fillna(0.0)


def _donchian_weights(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, window: int
) -> pd.DataFrame:
    upper = high.rolling(window).max().shift(1)
    breakout = close > upper
    # Keep it capped to a fixed number that are furthest above.
    dist = (close / upper - 1.0).where(breakout)
    top = _rank_pct(dist).ge(0.0)
    denom = top.sum(axis=1)
    return top.astype(float).div(denom, axis=0).fillna(0.0)


def _gap_fade_weights(
    open_: pd.DataFrame, close: pd.DataFrame, low: pd.DataFrame, gap_pct: float
) -> pd.DataFrame:
    prev_close = close.shift(1)
    gap = open_ / prev_close - 1.0
    signal = gap < gap_pct
    # Pick the lowest (most negative) gaps.
    ranked = _rank_pct(gap.where(signal))
    top = ranked.le(0.5)  # just keep lower half of the gap set
    denom = top.sum(axis=1)
    return top.astype(float).div(denom, axis=0).fillna(0.0)


def _low_vol_long_short_weights(
    close: pd.DataFrame, window: int, long_share: float, short_share: float
) -> pd.DataFrame:
    vol = _rolling_vol(close, window)
    rank = _rank_pct(vol)
    long = rank.ge(1.0 - long_share)
    short = rank.le(short_share)
    long_w = long.astype(float).div(long.sum(axis=1).replace(0, np.nan), axis=0)
    short_w = short.astype(float).div(short.sum(axis=1).replace(0, np.nan), axis=0)
    net = long_w.fillna(0.0) - short_w.fillna(0.0)
    net = net.div(net.abs().sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    return net


def _market_neutral_weights(
    close: pd.DataFrame,
    lookback: int,
    long_share: float,
    short_share: float,
) -> pd.DataFrame:
    mom = _momentum(close, lookback)
    rank = _rank_pct(mom)
    long = rank.ge(1.0 - long_share)
    short = rank.le(short_share)
    long_w = long.astype(float).div(long.sum(axis=1).replace(0, np.nan), axis=0)
    short_w = short.astype(float).div(short.sum(axis=1).replace(0, np.nan), axis=0)
    net = long_w.fillna(0.0) - short_w.fillna(0.0)
    net = net.div(net.abs().sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)
    return net


def _inverse_vol_equal_weights(close: pd.DataFrame, window: int) -> pd.DataFrame:
    vol = _rolling_vol(close, window)
    inv = 1.0 / vol.replace(0.0, np.nan)
    return inv.div(inv.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)


def _risk_parity_like_weights(
    close: pd.DataFrame, window: int, top_share: float
) -> pd.DataFrame:
    # Simple risk-balanced slice of low-vol universe.
    vol = _rolling_vol(close, window)
    rank = _rank_pct(vol)
    selected = rank.ge(1.0 - top_share)
    inv = 1.0 / vol.where(selected).replace(0.0, np.nan)
    return inv.div(inv.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)


def _scaled_strategy_weights(signal: pd.DataFrame, top_share: float) -> pd.DataFrame:
    ranked = _rank_pct(signal)
    top = ranked.ge(1.0 - top_share)
    denom = top.sum(axis=1)
    return top.astype(float).div(denom, axis=0).fillna(0.0)


def _make_specs() -> list[StrategySpec]:
    close_default = "close"
    specs: list[StrategySpec] = []

    def add(
        name: str,
        family: str,
        hypothesis: str,
        params: Mapping[str, object],
        fn: Callable[[Mapping[str, pd.DataFrame]], pd.DataFrame],
    ) -> None:
        specs.append(
            StrategySpec(name, family, hypothesis, params, fn)
        )

    # Cross-sectional momentum family (short and medium horizons)
    for lb in (20, 63, 126, 252):
        add(
            f"xs_mom_{lb}d",
            "cross_sectional_momentum",
            "Names that have outperformed over the recent horizon keep outperforming.",
            {"lookback": lb, "top_share": 0.25},
            lambda d, lb=lb: _cross_sectional_weights(
                _momentum(d[close_default], lb), 0.25
            ),
        )

    # Time-series momentum / trend following
    for lb in (20, 63, 126, 252):
        add(
            f"ts_mom_{lb}d",
            "time_series_momentum",
            "Individual asset trends persist; own only positive-trend names.",
            {"lookback": lb, "top_share": 0.25},
            lambda d, lb=lb: _time_series_momentum_weights(
                d[close_default], lb, 0.25, None
            ),
        )

    # Donchian / Turtle breakout
    for win in (20, 55, 120):
        add(
            f"donchian_{win}d",
            "trend_breakout",
            "New price highs continue because breakout entrants create persistence.",
            {"window": win, "top_share": 0.25},
            lambda d, win=win: _cross_sectional_weights(
                (d[close_default] / d["high"].rolling(win).max().shift(1) - 1.0)
                .where(d[close_default] > d["high"].rolling(win).max().shift(1)),
                0.25,
            ),
        )

    # Moving-average trend following
    for fast, slow in ((20, 50), (50, 200), (20, 100)):
        add(
            f"ma_trend_{fast}_{slow}",
            "ma_trend",
            "Fast MA above slow MA signals an uptrend.",
            {"fast": fast, "slow": slow, "top_share": 0.25},
            lambda d, fast=fast, slow=slow: _ma_trend_weights(
                d[close_default], fast, slow, 0.25
            ),
        )

    # RSI2 / short-term mean reversion
    for win, threshold in ((2, 20), (5, 30), (14, 30)):
        add(
            f"rsi_{win}_{int(threshold)}",
            "mean_reversion",
            "Extremely oversold names bounce back over short horizon.",
            {"window": win, "threshold": threshold, "top_share": 0.25},
            lambda d, win=win, threshold=threshold: _rsi2_weights(
                d[close_default], win, threshold, 0.25
            ),
        )

    # Bollinger mean reversion
    for win, num in ((20, 2.0), (30, 2.5), (50, 2.0)):
        add(
            f"boll_{win}_{num}",
            "mean_reversion",
            "Price below lower Bollinger band tends to revert upward.",
            {"window": win, "std": num, "top_share": 0.25},
            lambda d, win=win, num=num: _bollinger_weights(
                d[close_default], win, num, 0.25
            ),
        )

    # Low volatility / defensive factor (long only)
    for win in (20, 63, 126):
        add(
            f"lowvol_{win}d",
            "low_volatility",
            "Risk-adjusted returns favor low-volatility names in uncertain equity markets.",
            {"window": win, "top_share": 0.25},
            lambda d, win=win: _low_vol_weights(d[close_default], win, 0.25),
        )

    # Low-vol long-short (market neutral)
    add(
        "lowvol_ls",
        "low_volatility_long_short",
        "Long low-vol, short high-vol isolates the premium while hedging market risk.",
        {"window": 63, "long_share": 0.25, "short_share": 0.25},
        lambda d: _low_vol_long_short_weights(d[close_default], 63, 0.25, 0.25),
    )

    # Cross-sectional momentum long-short
    for lb in (63, 126, 252):
        add(
            f"mom_ls_{lb}d",
            "cross_sectional_momentum_long_short",
            "Winners vs losers spread is the classic cross-sectional momentum premium.",
            {"lookback": lb, "long_share": 0.25, "short_share": 0.25},
            lambda d, lb=lb: _market_neutral_weights(
                d[close_default], lb, 0.25, 0.25
            ),
        )

    # Risk parity / inverse vol equal weight
    for win in (20, 63, 126):
        add(
            f"invvol_{win}d",
            "inverse_volatility",
            "Risk-balanced low-vol allocation improves Sharpe without directional bets.",
            {"window": win},
            lambda d, win=win: _inverse_vol_equal_weights(d[close_default], win),
        )

    # Volatility targeting / risk-controlled cross-sectional momentum
    add(
        "mom_voltarget_63",
        "momentum_vol_target",
        "Trend signal scaled by inverse realized volatility.",
        {"lookback": 63, "top_share": 0.25, "vol_target": 0.20},
        lambda d: _time_series_momentum_weights(
            d[close_default], 63, 0.25, 0.20
        ),
    )

    # Gap fade
    add(
        "gap_fade_-1pct",
        "gap_reversion",
        "Negative opening gaps overreact and often reverse intraday / short-term.",
        {"gap_pct": -0.01, "top_share": 0.25},
        lambda d: _gap_fade_weights(
            d["open"], d[close_default], d["low"], -0.01
        ),
    )

    # Opening range breakout (simple close-based proxy for discovery)
    for factor in (0.25, 0.5, 1.0):
        add(
            f"orb_c_{factor}",
            "opening_range_breakout",
            "Break of the opening range identifies intraday directional continuation.",
            {"range_factor": factor, "top_share": 0.25},
            lambda d, factor=factor: _cross_sectional_weights(
                (d["close"] / d["high"].rolling(5).max().shift(1) - 1.0)
                .where(d["close"] > d["high"].rolling(5).max().shift(1)),
                0.25,
            ),
        )

    # Equal weight and buy-and-hold benchmarks
    def _equal(d: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
        close = d[close_default]
        return pd.DataFrame(
            1.0 / len(close.columns), index=close.index, columns=close.columns
        )

    def _buyhold(d: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
        close = d[close_default]
        n = len(close.columns)
        w = pd.DataFrame(
            1.0 / n, index=close.index, columns=close.columns
        )
        return w

    add("equal_weight", "benchmark", "Equal-weight all names in universe.", {}, _equal)
    add("buy_and_hold", "benchmark", "Equal-weight initial holdings never rebalanced.", {}, _buyhold)

    return specs


def build_specs() -> list[StrategySpec]:
    return _make_specs()


def run_specs(
    panels: Mapping[str, pd.DataFrame],
    specs: list[StrategySpec],
    *,
    config: DiscoveryConfig | None = None,
    mask: pd.DataFrame | None = None,
    strategy_names: list[str] | None = None,
) -> pd.DataFrame:
    """Run many specs and return a metric table."""
    rows = []
    selected = [s for s in specs if strategy_names is None or s.name in strategy_names]
    for spec in selected:
        try:
            weights = spec.generate(panels)
        except Exception as exc:  # keep discovery resilient
            rows.append(
                {
                    "strategy": spec.name,
                    "family": spec.family,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        if mask is not None:
            weights = weights.where(mask, 0.0)
        # long-only strategies may have sums that are zero; keep as-is for L/S
        # row-normalize only rows with nonzero gross (preserve gross on L/S).
        def _norm(row):
            s = row.sum()
            if s > 0 and (row >= 0).all():
                return row / s
            return row

        weights = weights.apply(_norm, axis=1)
        try:
            result = simulate(
                panels["close"],
                weights,
                config=config,
                metadata={"family": spec.family, "parameters": spec.parameters},
                strategy_name=spec.name,
            )
            row = {"strategy": spec.name, "family": spec.family, **result.metrics}
            rows.append(row)
        except Exception as exc:  # keep discovery resilient
            rows.append(
                {
                    "strategy": spec.name,
                    "family": spec.family,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return pd.DataFrame(rows).set_index("strategy")
