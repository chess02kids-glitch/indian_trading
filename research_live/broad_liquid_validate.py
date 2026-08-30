"""Re-validate the Momentum + Market-Regime strategy on a broad universe with a
proper, implementable liquidity filter (median daily traded value)."""
from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.broad_data import load_broad_universe
from research_live.metrics import Metrics, deflated_sharpe

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
IS_S, IS_E = "2010-01-01", "2018-12-31"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"
LIQ = float(sys.argv[1]) if len(sys.argv) > 1 else 2e7  # default ₹20M/day


def build_wide(uni, start, end):
    syms = list(uni.keys())
    closes, vals = {}, {}
    for s in syms:
        d = uni[s]
        d = d[(d.index >= start) & (d.index <= end)]
        if len(d) < 200:
            continue
        closes[s] = d["close"]
        if "value" in d.columns:
            vals[s] = d["value"].median()
        else:
            vals[s] = (d["volume"] * d["close"]).median()
    close = pd.DataFrame(closes).sort_index()
    return close, vals


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
    uni = load_broad_universe(min_years=8, min_avg_value=LIQ, start="2010-01-01")
    close, vals = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0.0)
    print(f"BROAD liquid universe (>=₹{LIQ/1e6:.0f}M/day): {close.shape[1]} names x {len(close)} days\n")

    n = close.notna().sum(axis=1).replace(0, np.nan)
    ew_w = close.notna().astype(float).div(n, axis=0).fillna(0)
    pr_ew = (ew_w * ret).sum(axis=1)
    m_ew = Metrics.from_returns(pr_ew, (1 + pr_ew).cumprod())
    print(f"EW benchmark: Sharpe={m_ew.sharpe:.2f} CAGR={m_ew.cagr:.2f} MDD={m_ew.max_dd:.2f}")

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
    df.to_csv("research_live/broad_liquid_grid.csv", index=False)
    p = df.pivot_table(index="ma", columns="lb", values="oos_sh", aggfunc="mean")
    print("\nOOS Sharpe (mean over top_n), by MA x lookback:")
    print(p.round(2).to_string())
    print(f"\nGrid OOS Sharpe: mean={df['oos_sh'].mean():.2f} std={df['oos_sh'].std():.2f} "
          f"min={df['oos_sh'].min():.2f} max={df['oos_sh'].max():.2f}")
    print(f"fraction OOS Sharpe>1.0: {(df['oos_sh']>1.0).mean():.0%}")
    print(f"fraction OOS Sharpe>1.2: {(df['oos_sh']>1.2).mean():.0%}")

    # chosen config
    cfg = dict(lookback=20, hold=20, top_n=20, ma=100)
    tgt = strategy_tgt(close, **cfg).shift(1).fillna(0.0)
    pr, eq = sim(ret, tgt)
    io = pr.index >= pd.Timestamp(OOS_S)
    for lab, mm in [("FULL", Metrics.from_returns(pr, eq)),
                    ("IS", Metrics.from_returns(pr[~io], eq[~io])),
                    ("OOS", Metrics.from_returns(pr[io], eq[io]))]:
        print(f"  {cfg} {lab}: Sharpe={mm.sharpe:.2f} CAGR={mm.cagr:.2f} Calmar={mm.calmar:.2f} "
              f"MDD={mm.max_dd:.2f} PF={mm.profit_factor:.2f} Sortino={mm.sortino:.2f}")
    mo = Metrics.from_returns(pr[io], eq[io])
    rng = np.random.default_rng(7)
    oos = pr[io].values
    bs = [(lambda b: b.mean()/b.std()*np.sqrt(252) if b.std()>0 else 0)(rng.choice(oos,len(oos),True)) for _ in range(2000)]
    print(f"  Bootstrap OOS Sharpe mean={np.mean(bs):.2f} 95% CI=[{np.percentile(bs,2.5):.2f},{np.percentile(bs,97.5):.2f}]")
    print(f"  Deflated Sharpe: {deflated_sharpe(mo.sharpe,int(io.sum()),200):.3f}")
    eq.to_csv("research_live/broad_liquid_equity.csv", header=["equity"])
    (eq/eq.cummax()-1).to_csv("research_live/broad_liquid_drawdown.csv", header=["dd"])


if __name__ == "__main__":
    main()
