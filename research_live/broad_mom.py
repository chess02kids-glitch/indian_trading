"""Test cross-sectional momentum on a broad split-adjusted universe
(large/mid/small caps) with proper 1-day lag and costs."""
from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.broad_data import load_broad_universe
from research_live.runner import StrategyRunner
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
IS_S, IS_E = "2010-01-01", "2018-12-31"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"


def build_wide(uni, start, end):
    syms = list(uni.keys())
    closes = {}
    opens = {}
    highs = {}
    lows = {}
    for s in syms:
        d = uni[s]
        d = d[(d.index >= start) & (d.index <= end)]
        if len(d) < 200:
            continue
        closes[s] = d["close"]
        if "open" in d.columns:
            opens[s] = d["open"]
        highs[s] = d["high"]
        lows[s] = d["low"]
    close = pd.DataFrame(closes).sort_index()
    high = pd.DataFrame(highs).sort_index()
    low = pd.DataFrame(lows).sort_index()
    open_ = pd.DataFrame(opens).sort_index()
    return close, high, low, open_


def mom_top(close, high, low, open_, lookback=60, hold=20, top_n=20):
    mom = close.pct_change(lookback)
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < top_n + 5:
            continue
        top = m.nlargest(top_n).index
        for s in top:
            rebal.loc[dt, s] = 1.0 / top_n
    return rebal.ffill().fillna(0.0)


def ew_benchmark(close, hold=20):
    ret = close.pct_change().fillna(0.0)
    # count non-nan per day for equal weight
    n = close.notna().sum(axis=1)
    tgt = close.notna().astype(float).div(n, axis=0)
    return ret, tgt


def main():
    uni = load_broad_universe(min_years=8, min_avg_value=5e6, start="2010-01-01")
    close, high, low, open_ = build_wide(uni, S, E)
    print(f"wide universe: {close.shape[1]} names x {len(close)} days")
    ret = close.pct_change().fillna(0.0)

    # EW benchmark (daily-rebalanced, no cost) for reference
    n = close.notna().sum(axis=1).replace(0, np.nan)
    ew_w = close.notna().astype(float).div(n, axis=0).fillna(0)
    pr = (ew_w * ret).sum(axis=1)
    m_ew = Metrics.from_returns(pr, (1 + pr).cumprod())
    print(f"EW benchmark (no cost): Sharpe={m_ew.sharpe:.2f} CAGR={m_ew.cagr:.2f} MDD={m_ew.max_dd:.2f}\n")

    runner = StrategyRunner(None, close, high, low, open_, cost_oneway=COST)
    # Runner.simulate needs panel-based ret; replicate manually
    def sim(tgt):
        r = ret.reindex_like(tgt).fillna(0.0).values
        t = np.clip(tgt.fillna(0).values, 0, 1)
        g = t.sum(axis=1, keepdims=True)
        t = t * np.minimum(1.0 / np.maximum(g, 1e-12), 1.0)
        pr_ = (t * r).sum(axis=1)
        tgt_next = np.vstack([t[1:], t[-1:]])
        w_end = t * (1 + r); w_end = w_end / np.maximum(1 + pr_[:, None], 1e-12)
        turn = np.abs(tgt_next - w_end).sum(axis=1)
        net = (1 + pr_) - turn * COST
        eq = np.cumprod(net)
        return pd.Series(net - 1, index=ret.index), pd.Series(eq, index=ret.index)

    rows = []
    for lb, hold, tn in itertools.product([20, 60, 120, 250], [20], [20, 30, 40]):
        tgt = mom_top(close, high, low, open_, lb, hold, tn).shift(1).fillna(0.0)
        pr, eq = sim(tgt)
        idx_is = pr.index <= pd.Timestamp(IS_E)
        m_is = Metrics.from_returns(pr[idx_is], eq[idx_is])
        idx_oos = pr.index >= pd.Timestamp(OOS_S)
        m_oos = Metrics.from_returns(pr[idx_oos], eq[idx_oos])
        rows.append(dict(lb=lb, hold=hold, tn=tn, is_sh=m_is.sharpe, oos_sh=m_oos.sharpe,
                         oos_cagr=m_oos.cagr, oos_mdd=m_oos.max_dd))
        print(f"mom lb={lb:4d} tn={tn} | IS sh={m_is.sharpe:.2f} | OOS sh={m_oos.sharpe:.2f} "
              f"cagr={m_oos.cagr:.2f} mdd={m_oos.max_dd:.2f}")
    df = pd.DataFrame(rows).sort_values("oos_sh", ascending=False)
    df.to_csv("research_live/broad_mom.csv", index=False)
    print("\nSorted by OOS Sharpe (EW bench=%.2f):" % m_ew.sharpe)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
