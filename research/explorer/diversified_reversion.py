"""Diversified cross-sectional mean-reversion / reversal screen.

The concentrated Bollinger variants (15/3.0, 20/2.5, 30/2.5) hold 1-2 names and
are not a robust investable profile. This module tests whether a *diversified*
monthly short-term-reversion screen (always hold up to K names, use a generous
deviation/RSI gate so there are candidates) preserves any edge.

Implementations:
* ``z_screen``: rank by rolling z-score; long the K most negative (below a
  gate); optionally short the K most positive (above a gate) for a net-zero
  long-short.
* ``rsi_screen``: long the K lowest RSI below a gate.

All portfolios use explicit K (minimum/maximum names) and equal weight, so the
exposure is not a single 100% position. Cost is charged on turnover. PIT
membership mask is applied. Results include a market-neutral long-short to
isolate cross-sectional reversal from market beta.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .io import (
    load_eod_panels,
    load_pit_universe,
    panel_universe_mask,
    resolve_research_universe,
)
from .sim import DiscoveryConfig, simulate

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "data" / "features"


def _rank(frame):
    return frame.rank(axis=1, pct=True, method="first")


def _rsi(close, window):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    al = loss.ewm(alpha=1.0 / window, adjust=False).mean()
    rs = ag / al.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _z(close, window):
    sma = close.rolling(window).mean()
    sd = close.rolling(window).std()
    return (close - sma) / sd.replace(0.0, np.nan)


def long_only_z(
    close, window, gate, k
):
    z = _z(close, window)
    weight = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    # For each date, pick the K eligible names with the smallest z.
    # not row-vectorized: build per rebalance. But we generate daily weights for
    # discovery engine which samples at month-end. We'll build each date.
    for ts in close.index:
        row = z.loc[ts]
        elig = row[row < gate]
        if len(elig) == 0:
            continue
        picks = elig.sort_values(ascending=True).head(k).index
        weight.loc[ts, picks] = 1.0 / len(picks)
    return weight


def long_short_z(close, window, gate, k):
    z = _z(close, window)
    weight = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for ts in close.index:
        row = z.loc[ts]
        long_picks = row[row < -gate].sort_values(ascending=True).head(k).index
        short_picks = row[row > gate].sort_values(ascending=False).head(k).index
        if len(long_picks):
            weight.loc[ts, long_picks] += 1.0 / len(long_picks)
        if len(short_picks):
            weight.loc[ts, short_picks] -= 1.0 / len(short_picks)
        gross = weight.loc[ts].abs().sum()
        if gross > 0:
            weight.loc[ts] = weight.loc[ts] / gross
    return weight


def long_only_rsi(close, window, gate, k):
    rsi = _rsi(close, window)
    weight = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for ts in close.index:
        row = rsi.loc[ts]
        elig = row[row < gate]
        if len(elig) == 0:
            continue
        picks = elig.sort_values(ascending=True).head(k).index
        weight.loc[ts, picks] = 1.0 / len(picks)
    return weight


def _daily_metrics(returns):
    returns = returns.dropna()
    if returns.empty:
        return {}
    eq = (1.0 + returns).cumprod()
    total = float(eq.iloc[-1] - 1.0)
    ann = float((1.0 + total) ** (252.0 / len(returns)) - 1.0)
    vol = float(returns.std(ddof=1) * np.sqrt(252))
    sharpe = float(ann / vol) if vol > 0 else 0.0
    dd = eq / eq.cummax() - 1.0
    return {
        "total_return": total,
        "cagr": ann,
        "volatility": vol,
        "sharpe": sharpe,
        "max_drawdown": float(dd.min()),
        "periods": float(len(returns)),
    }


def _norm_long(row):
    s = row.sum()
    if s > 0 and (row >= 0).all():
        return row / s
    return row


def main():
    panels = load_eod_panels()
    close = panels["close"]
    universe = load_pit_universe()
    avail = resolve_research_universe(close, universe)
    close = close.loc[:, [c for c in close.columns if c in avail]]
    mask = panel_universe_mask(universe, close.index, close.columns)
    config = DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12)

    eq = pd.DataFrame(1.0 / len(close.columns), index=close.index, columns=close.columns)
    eq_res = simulate(close, eq, config=config)
    market_ret = eq_res.returns

    rows = []
    for window in (5, 10, 20):
        for gate in (1.5, 2.0, 2.5):
            for k in (5, 10, 20):
                # long-only z screen
                w = long_only_z(close, window, -gate, k).where(mask, 0.0)
                w = w.apply(_norm_long, axis=1)
                res = simulate(close, w, config=config)
                m = _daily_metrics(res.returns)
                m.update({"mode": "z_long_only", "window": window, "gate": gate, "k": k})
                m["avg_positions"] = float(w.gt(1e-9).sum(axis=1)[w.gt(1e-9).sum(axis=1) > 0].mean())
                m["max_positions"] = float(w.gt(1e-9).sum(axis=1).max())
                m["beta"] = float(
                    np.cov(pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["s"],
                           pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["m"])[0, 1]
                    / np.var(market_ret.dropna())
                )
                m["annualized_alpha"] = float((pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["s"]
                                              - (np.cov(pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["s"],
                                                         pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["m"])[0, 1]
                                                     / np.var(market_ret.dropna()))
                                              * pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["m"]).mean() * 252)
                rows.append(m)

    # market-neutral long-short z (diversified)
    for window in (5, 10, 20):
        for gate in (1.5, 2.0, 2.5):
            for k in (5, 10, 20):
                w = long_short_z(close, window, gate, k).where(mask, 0.0)
                res = simulate(close, w, config=config)
                m = _daily_metrics(res.returns)
                m.update({"mode": "z_long_short", "window": window, "gate": gate, "k": k})
                m["avg_positions"] = float(w.gt(1e-9).sum(axis=1)[w.gt(1e-9).sum(axis=1) > 0].mean())
                m["max_positions"] = float(w.gt(1e-9).sum(axis=1).max())
                m["beta"] = float(
                    np.cov(pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["s"],
                           pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["m"])[0, 1]
                    / np.var(market_ret.dropna())
                )
                rows.append(m)

    # RSI diversified long-only
    for window in (2, 5, 14):
        for gate in (20.0, 30.0, 35.0):
            for k in (5, 10, 20):
                w = long_only_rsi(close, window, gate, k).where(mask, 0.0)
                w = w.apply(_norm_long, axis=1)
                res = simulate(close, w, config=config)
                m = _daily_metrics(res.returns)
                m.update({"mode": "rsi_long_only", "window": window, "gate": gate, "k": k})
                m["avg_positions"] = float(w.gt(1e-9).sum(axis=1)[w.gt(1e-9).sum(axis=1) > 0].mean())
                m["max_positions"] = float(w.gt(1e-9).sum(axis=1).max())
                m["beta"] = float(
                    np.cov(pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["s"],
                           pd.concat([res.returns.rename("s"), market_ret.rename("m")], axis=1).dropna()["m"])[0, 1]
                    / np.var(market_ret.dropna())
                )
                rows.append(m)

    frame = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    frame.to_json(FEATURES_DIR / "diversified_reversion.json", orient="records", indent=2)
    pd.set_option("display.width", 250)
    print(frame[["mode", "window", "gate", "k", "cagr", "volatility", "sharpe", "max_drawdown",
                 "avg_positions", "max_positions", "beta", "annualized_alpha"]].head(60).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
