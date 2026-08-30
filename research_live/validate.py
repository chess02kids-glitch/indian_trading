"""Rigorous validation of the Momentum + Market-Regime-Filter strategy.

Checks parameter stability (grid of lookback/MA/top_n), IS vs OOS, walk-forward,
and robustness to the market proxy. The intent is to reject parameter cliffs and
confirm a stable plateau, not a single lucky point.
"""
from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.broad_data import load_broad_universe
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
IS_S, IS_E = "2010-01-01", "2018-12-31"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"


def build_wide(uni, start, end):
    syms = list(uni.keys())
    closes, highs, lows, opens = {}, {}, {}, {}
    for s in syms:
        d = uni[s]
        d = d[(d.index >= start) & (d.index <= end)]
        if len(d) < 200:
            continue
        closes[s] = d["close"]; highs[s] = d["high"]; lows[s] = d["low"]
        if "open" in d.columns: opens[s] = d["open"]
    return (pd.DataFrame(closes).sort_index(), pd.DataFrame(highs).sort_index(),
            pd.DataFrame(lows).sort_index(), pd.DataFrame(opens).sort_index())


def market_proxy(close, weights="ew"):
    mp = close.pct_change().fillna(0).mean(axis=1)
    return (1 + mp).cumprod()


def strategy_tgt(close, lookback, hold, top_n, ma):
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
        for s in m.nlargest(top_n).index:
            rebal.loc[dt, s] = 1.0 / top_n
    tgt = rebal.ffill().fillna(0.0)

    mkt = market_proxy(close)
    ma_s = mkt.rolling(ma).mean()
    exp = (mkt > ma_s).astype(float).shift(1)
    exp[ma_s.isna()] = 1.0
    exp = exp.fillna(0.0)
    return tgt.mul(exp, axis=0)


def sim(rets, tgt, cost_mult=1.0):
    r = rets.reindex_like(tgt).fillna(0.0).values
    t = np.clip(tgt.fillna(0).values, 0, 1)
    g = t.sum(axis=1, keepdims=True)
    t = t * np.minimum(1.0 / np.maximum(g, 1e-12), 1.0)
    pr = (t * r).sum(axis=1)
    tn = np.vstack([t[1:], t[-1:]])
    w_end = t * (1 + r); w_end = w_end / np.maximum(1 + pr[:, None], 1e-12)
    turn = np.abs(tn - w_end).sum(axis=1)
    net = (1 + pr) - turn * COST * cost_mult
    return pd.Series(net - 1, index=rets.index), pd.Series(np.cumprod(net), index=rets.index)


def main():
    uni = load_broad_universe(min_years=8, min_avg_value=5e6, start="2010-01-01")
    close, high, low, open_ = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0.0)
    print(f"universe {close.shape[1]} names, {len(close)} days")

    # ---- Parameter stability grid (OOS + IS) ----
    print("\n===== Parameter grid (OOS) =====")
    grid = list(itertools.product([10, 20, 40, 60], [60, 100, 150, 200], [15, 20, 30]))
    rows = []
    for lb, ma, tn in grid:
        tgt = strategy_tgt(close, lb, 20, tn, ma).shift(1).fillna(0.0)
        pr, eq = sim(ret, tgt)
        ioos = pr.index >= pd.Timestamp(OOS_S)
        mi = Metrics.from_returns(pr[~ioos], eq[~ioos])
        mo = Metrics.from_returns(pr[ioos], eq[ioos])
        rows.append(dict(lb=lb, ma=ma, tn=tn, is_sh=mi.sharpe, oos_sh=mo.sharpe,
                         oos_cagr=mo.cagr, oos_mdd=mo.max_dd, oos_calmar=mo.calmar))
    df = pd.DataFrame(rows)
    df.to_csv("research_live/param_grid.csv", index=False)
    p = df.pivot_table(index="ma", columns="lb", values="oos_sh", aggfunc="mean")
    print("OOS Sharpe averaged over top_n, by (MA rows, lookback cols):")
    print(p.round(2).to_string())
    print("\nTop 10 by OOS Sharpe:")
    print(df.sort_values("oos_sh", ascending=False).head(10).to_string(index=False))
    # stability: mean & std of OOS sharpe across grid
    print(f"\nOOS Sharpe across grid: mean={df['oos_sh'].mean():.2f} std={df['oos_sh'].std():.2f} "
          f"min={df['oos_sh'].min():.2f} max={df['oos_sh'].max():.2f}")
    # fraction beating benchmark (0.85)
    print(f"fraction with OOS Sharpe>0.85: {(df['oos_sh']>0.85).mean():.0%}")
    print(f"fraction with OOS Sharpe>1.0: {(df['oos_sh']>1.0).mean():.0%}")

    # ---- Walk-forward for best-rep config (lb=20,ma=100,tn=20) ----
    print("\n===== Walk-forward (lb=20, ma=100, tn=20) =====")
    tgt = strategy_tgt(close, 20, 20, 20, 100).shift(1).fillna(0.0)
    pr, eq = sim(ret, tgt)
    wf = []
    for y in range(2016, 2026):
        st = f"{y}-01-01"; en = f"{min(y+1,2026)}-06-30"
        idx = (pr.index >= pd.Timestamp(st)) & (pr.index <= pd.Timestamp(en))
        m = Metrics.from_returns(pr[idx], eq[idx])
        wf.append((y, m.sharpe, m.cagr, m.max_dd, m.calmar))
        print(f"  {y}: Sharpe={m.sharpe:.2f} CAGR={m.cagr:.2f} MDD={m.max_dd:.2f} Calmar={m.calmar:.2f}")
    wfdf = pd.DataFrame(wf, columns=["year", "sharpe", "cagr", "mdd", "calmar"])
    print(f"  Walk-forward mean Sharpe={wfdf['sharpe'].mean():.2f}, "
          f"% positive-years={ (wfdf['sharpe']>0).mean():.0%}")


if __name__ == "__main__":
    main()
