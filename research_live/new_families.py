"""Test genuinely new strategy families:
1. Pairs trading / statistical arbitrage (dollar-neutral z-score spread mean reversion)
2. Price action (overnight-gap fade, weekly)
All on the clean large-cap universe with realistic costs & 1-day lag.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2009-01-01", "2026-06-30"
IS_S, IS_E = "2009-01-01", "2017-12-31"
OOS_S, OOS_E = "2018-01-01", "2026-06-30"


def load():
    panel = load_panel()
    syms = liquid_universe(panel, "2008-01-01", 0.9)
    sub, close = align_panel(panel, syms, S, E)
    open_ = sub["open"].unstack("symbol")
    high = sub["high"].unstack("symbol")
    low = sub["low"].unstack("symbol")
    ret = close.pct_change().fillna(0.0)
    return close, open_, high, low, ret


def sim_longshort(rets, w):
    """Simulate dollar-neutral long-short book with given weights (sum~0)."""
    r = rets.reindex_like(w).fillna(0.0).values
    wt = w.fillna(0.0).values
    g = np.abs(wt).sum(axis=1, keepdims=True)
    wt = wt / np.maximum(g, 1e-12) * 1.0  # gross=1.0 (0.5 long 0.5 short)
    pr = (wt * r).sum(axis=1)
    w_end = wt * (1 + r) / np.maximum(1 + pr[:, None], 1e-12)
    w_next = np.vstack([wt[1:], wt[-1:]])
    turn = np.abs(w_next - w_end).sum(axis=1)
    net = (1 + pr) - turn * COST
    return pd.Series(net - 1, index=rets.index), pd.Series(np.cumprod(net), index=rets.index)


def pairs_strategy(close, ret, spread_n=60, z_entry=2.0, z_exit=0.5, top_pairs=30,
                   corr_win=250, corr_min=0.7):
    """Global pairs: rank pairs by rolling correlation, trade those whose
    (normalized) price spread z-score is extreme, mean-revert. Rebalanced daily
    on the highest-dispersion pairs."""
    logp = np.log(close)
    # build target weights each day using info up to that day (shift 1 for lag)
    dates = close.index
    w = pd.DataFrame(0.0, index=dates, columns=close.columns)
    # precompute all pairwise rolling z-scores lazily on a subset for speed
    syms = close.columns.tolist()
    n = len(syms)
    # sample pairs: use correlation to pick pairs once per month
    rc = close.pct_change().rolling(corr_win).corr()
    prev_reb = 0
    # Use a monthly pair-selection approach
    rebal_dates = dates[::20]
    pair_pool = {}
    for k in range(len(dates)):
        dt = dates[k]
        if dt in rebal_dates:
            corr = rc.loc[dt]
            cvals = corr.values
            pairs = []
            for i in range(n):
                for j in range(i + 1, n):
                    c = cvals[i, j]
                    if np.isfinite(c) and c > corr_min:
                        pairs.append((c, i, j))
            pairs.sort(reverse=True)
            pair_pool = pairs[:top_pairs]
        if not pair_pool:
            continue
        day_w = np.zeros(n)
        lpi = logp.loc[dt].values
        # for each selected pair, compute z of relative price
        for (c, i, j) in pair_pool:
            ratio = lpi[i] - lpi[j]  # log ratio
            hist = (logp[syms[i]] - logp[syms[j]]).loc[:dt]
            hist = hist.dropna().values
            if len(hist) < spread_n:
                continue
            mean = hist[-spread_n:].mean()
            std = hist[-spread_n:].std()
            if std < 1e-8:
                continue
            z = (ratio - mean) / std
            if z < -z_entry:
                day_w[i] += 1.0; day_w[j] -= 1.0
            elif z > z_entry:
                day_w[i] -= 1.0; day_w[j] += 1.0
        # normalize: cap gross
        if day_w.sum() != 0:
            day_w = day_w / np.abs(day_w).sum() * (len(pair_pool) / 10.0)
        w.loc[dt] = day_w
    w = w.shift(1).fillna(0.0)  # execution lag
    return w


def gap_fade(close, open_, ret):
    """Fade the overnight gap: short names that gap up sharply, long names that
    gap down sharply, weekly rebalance. (Intraday reversion of opening gaps.)"""
    gap = open_ / close.shift(1) - 1
    dates = close.index
    w = pd.DataFrame(0.0, index=dates, columns=close.columns)
    for d in range(0, len(dates), 5):
        dt = dates[d]
        g = gap.loc[dt].dropna()
        if len(g) < 20:
            continue
        up = g.nlargest(8).index
        dn = g.nsmallest(8).index
        for s in up:
            w.loc[dt, s] = -1.0 / 16
        for s in dn:
            w.loc[dt, s] = 1.0 / 16
    return w.shift(1).fillna(0.0)


def main():
    close, open_, high, low, ret = load()
    print(f"universe {close.shape[1]} names")

    print("\n===== Pairs trading / stat-arb (OOS) =====")
    # pairs is slow; run full once
    w = pairs_strategy(close, ret)
    pr, eq = sim_longshort(ret, w)
    io = pr.index <= pd.Timestamp(IS_E)
    mi = Metrics.from_returns(pr[io], eq[io])
    mo = Metrics.from_returns(pr[~io], eq[~io])
    print(f"  Pairs: IS Sharpe={mi.sharpe:.2f} CAGR={mi.cagr:.2f} | "
          f"OOS Sharpe={mo.sharpe:.2f} CAGR={mo.cagr:.2f} MDD={mo.max_dd:.2f}")

    print("\n===== Gap fade (weekly) =====")
    w = gap_fade(close, open_, ret)
    pr, eq = sim_longshort(ret, w)
    io = pr.index <= pd.Timestamp(IS_E)
    mi = Metrics.from_returns(pr[io], eq[io])
    mo = Metrics.from_returns(pr[~io], eq[~io])
    print(f"  Gap-fade: IS Sharpe={mi.sharpe:.2f} CAGR={mi.cagr:.2f} | "
          f"OOS Sharpe={mo.sharpe:.2f} CAGR={mo.cagr:.2f} MDD={mo.max_dd:.2f}")


if __name__ == "__main__":
    main()
