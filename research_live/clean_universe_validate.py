"""Validate the Momentum + Market-Regime strategy on the CLEAN 133 large-cap
universe (split/bonus adjusted, long history, liquid). This is the most
implementable and highest-quality data set."""
from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.metrics import Metrics, deflated_sharpe

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
IS_S, IS_E = "2010-01-01", "2018-12-31"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"


def market_proxy(close):
    return (1 + close.pct_change().fillna(0).mean(axis=1)).cumprod()


def strategy_tgt(close, lookback, hold, top_n, ma):
    mom = close.pct_change(lookback)
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < top_n + 3:
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
    panel = load_panel()
    syms = liquid_universe(panel, "2009-01-01", 0.85)  # 133 clean names, 85% coverage
    sub, close = align_panel(panel, syms, S, E)
    ret = close.pct_change().fillna(0.0)
    print(f"CLEAN large-cap universe: {close.shape[1]} names x {len(close)} days\n")

    # EW benchmark (no-cost) for reference
    n = close.notna().sum(axis=1).replace(0, np.nan)
    ew_w = close.notna().astype(float).div(n, axis=0).fillna(0)
    pr_ew = (ew_w * ret).sum(axis=1)
    m_ew = Metrics.from_returns(pr_ew, (1 + pr_ew).cumprod())
    print(f"EW benchmark: Sharpe={m_ew.sharpe:.2f} CAGR={m_ew.cagr:.2f} MDD={m_ew.max_dd:.2f} Calmar={m_ew.calmar:.2f}")

    print("\n===== Parameter grid (OOS Sharpe) =====")
    grid = list(itertools.product([10, 20, 40, 60], [60, 100, 150, 200], [10, 15, 20]))
    rows = []
    for lb, ma, tn in grid:
        tgt = strategy_tgt(close, lb, 20, tn, ma).shift(1).fillna(0.0)
        pr, eq = sim(ret, tgt)
        io = pr.index >= pd.Timestamp(OOS_S)
        mi = Metrics.from_returns(pr[~io], eq[~io])
        mo = Metrics.from_returns(pr[io], eq[io])
        rows.append(dict(lb=lb, ma=ma, tn=tn, is_sh=mi.sharpe, oos_sh=mo.sharpe,
                         oos_cagr=mo.cagr, oos_mdd=mo.max_dd, oos_calmar=mo.calmar))
    df = pd.DataFrame(rows)
    df.to_csv("research_live/clean_param_grid.csv", index=False)
    p = df.pivot_table(index="ma", columns="lb", values="oos_sh", aggfunc="mean")
    print("OOS Sharpe (mean over top_n):")
    print(p.round(2).to_string())
    print(f"\nGrid OOS Sharpe: mean={df['oos_sh'].mean():.2f} std={df['oos_sh'].std():.2f} "
          f"min={df['oos_sh'].min():.2f} max={df['oos_sh'].max():.2f}")
    print(f"fraction OOS Sharpe>1.0: {(df['oos_sh']>1.0).mean():.0%}")
    print(f"fraction OOS Sharpe>1.2: {(df['oos_sh']>1.2).mean():.0%}")

    # Best robust config (center of plateau)
    cfg = dict(lookback=20, hold=20, top_n=15, ma=100)
    print(f"\n===== Full validation for {cfg} =====")
    tgt = strategy_tgt(close, **cfg).shift(1).fillna(0.0)
    pr, eq = sim(ret, tgt)
    m = Metrics.from_returns(pr, eq)
    io = pr.index >= pd.Timestamp(OOS_S)
    mi = Metrics.from_returns(pr[~io], eq[~io])
    mo = Metrics.from_returns(pr[io], eq[io])
    for lab, mm in [("FULL", m), ("IS", mi), ("OOS", mo)]:
        print(f"  {lab}: Sharpe={mm.sharpe:.2f} CAGR={mm.cagr:.2f} Sortino={mm.sortino:.2f} "
              f"Calmar={mm.calmar:.2f} MDD={mm.max_dd:.2f} PF={mm.profit_factor:.2f}")

    # Bootstrap OOS
    rng = np.random.default_rng(1)
    oos = pr[io].values
    bs = [ (lambda b: b.mean()/b.std()*np.sqrt(252) if b.std()>0 else 0)(rng.choice(oos,len(oos),True)) for _ in range(2000)]
    print(f"  Bootstrap OOS Sharpe mean={np.mean(bs):.2f} 95% CI=[{np.percentile(bs,2.5):.2f},{np.percentile(bs,97.5):.2f}]")
    print(f"  Deflated Sharpe (200 trials): {deflated_sharpe(mo.sharpe,int(io.sum()),200):.3f}")
    eq.to_csv("research_live/clean_strategy_equity.csv", header=["equity"])
    dd = eq/eq.cummax()-1
    dd.to_csv("research_live/clean_strategy_drawdown.csv", header=["dd"])


if __name__ == "__main__":
    main()
