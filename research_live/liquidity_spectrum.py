"""Map strategy edge vs universe liquidity threshold."""
import sys
sys.path.insert(0, "/home/user/indian_trading")
import numpy as np, pandas as pd
from research_live.broad_data import load_broad_universe
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
OOS_S = "2019-01-01"


def build_wide(uni, start, end):
    closes = {}
    for s in uni:
        d = uni[s]
        d = d[(d.index >= start) & (d.index <= end)]
        if len(d) < 200:
            continue
        closes[s] = d["close"]
    return pd.DataFrame(closes).sort_index()


def strat(close, lb, hold, tn, ma):
    mom = close.pct_change(lb)
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for d in range(0, len(close), hold):
        dt = close.index[d]
        m = mom.loc[dt].dropna()
        if len(m) < tn + 3:
            continue
        for s in m.nlargest(tn).index:
            rebal.loc[dt, s] = 1.0 / tn
    tgt = rebal.ffill().fillna(0.0)
    mkt = (1 + close.pct_change().fillna(0).mean(axis=1)).cumprod()
    ma_s = mkt.rolling(ma).mean()
    exp = (mkt > ma_s).astype(float).shift(1)
    exp[ma_s.isna()] = 1.0
    exp = exp.fillna(0.0)
    return tgt.mul(exp, axis=0)


def sim(rets, tgt):
    r = rets.reindex_like(tgt).fillna(0).values
    t = np.clip(tgt.fillna(0).values, 0, 1)
    g = t.sum(axis=1, keepdims=True)
    t = t * np.minimum(1.0 / np.maximum(g, 1e-12), 1.0)
    pr = (t * r).sum(axis=1)
    tn = np.vstack([t[1:], t[-1:]])
    w = t * (1 + r); w = w / np.maximum(1 + pr[:, None], 1e-12)
    turn = np.abs(tn - w).sum(axis=1)
    net = (1 + pr) - turn * COST
    return pd.Series(net - 1, index=rets.index), pd.Series(np.cumprod(net), index=rets.index)


for liq in [5e6, 1e7, 2e7, 5e7, 1e8]:
    uni = load_broad_universe(min_years=8, min_avg_value=liq, start="2010-01-01")
    close = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0)
    tgt = strat(close, 20, 20, 20, 100).shift(1).fillna(0)
    pr, eq = sim(ret, tgt)
    io = pr.index >= pd.Timestamp(OOS_S)
    mo = Metrics.from_returns(pr[io], eq[io])
    mf = Metrics.from_returns(pr, eq)
    n = close.notna().sum(axis=1).replace(0, np.nan)
    ew = close.notna().astype(float).div(n, axis=0).fillna(0)
    me = Metrics.from_returns((ew * ret).sum(axis=1), (1 + (ew * ret).sum(axis=1)).cumprod())
    print(f"LIQ=Rs{liq/1e6:.0f}M names={close.shape[1]:4d} | "
          f"FULL sh={mf.sharpe:.2f} | OOS sh={mo.sharpe:.2f} cagr={mo.cagr:.2f} "
          f"mdd={mo.max_dd:.2f} calmar={mo.calmar:.2f} | EW sh={me.sharpe:.2f} (out_by={(mo.sharpe-me.sharpe):+.2f})")
