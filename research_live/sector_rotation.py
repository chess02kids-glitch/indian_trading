"""Sector rotation via momentum on Nifty sector indices, long-only, with
optional market regime filter. Tests whether rotating across sectors beats
equal-weight across sectors (net of costs)."""
from __future__ import annotations

import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
IS_S, IS_E = "2010-01-01", "2018-12-31"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"
DAILY = os.path.join("data", "eod2", "daily")

# major tradable sector indices
SECTORS = ["nifty auto", "nifty bank", "nifty energy", "nifty fmcg", "nifty it",
           "nifty metal", "nifty pharma", "nifty realty", "nifty oil & gas",
           "nifty consumer durables", "nifty media", "nifty infrastructure",
           "nifty psu bank", "nifty private bank", "nifty power",
           "nifty capital goods", "nifty chemicals", "nifty cement"]


def load_sectors():
    closes = {}
    for name in SECTORS:
        p = os.path.join(DAILY, name + ".csv")
        if not os.path.exists(p):
            continue
        d = pd.read_csv(p)
        d.columns = [c.strip().lower() for c in d.columns]
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d = d.dropna(subset=["date", "close"]).set_index("date")["close"].sort_index()
        closes[name] = d
    return pd.DataFrame(closes).sort_index().loc[S:E]


def sim(rets, tgt):
    r = rets.reindex_like(tgt).fillna(0.0).values
    t = np.clip(tgt.fillna(0).values, 0, 1)
    g = t.sum(axis=1, keepdims=True)
    t = t * np.minimum(1.0 / np.maximum(g, 1e-12), 1.0)
    pr = (t * r).sum(axis=1)
    tn = np.vstack([t[1:], t[-1:]])
    w_end = t * (1 + r) / np.maximum(1 + pr[:, None], 1e-12)
    turn = np.abs(tn - w_end).sum(axis=1)
    net = (1 + pr) - turn * COST
    return pd.Series(net - 1, index=rets.index), pd.Series(np.cumprod(net), index=rets.index)


def sector_rotation(sec, lookback=60, hold=20, top_k=4, use_regime=True, ma=100):
    mom = sec.pct_change(lookback)
    rebal = pd.DataFrame(np.nan, index=sec.index, columns=sec.columns)
    dates = sec.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < top_k:
            continue
        for s in m.nlargest(top_k).index:
            rebal.loc[dt, s] = 1.0 / top_k
    tgt = rebal.ffill().fillna(0.0)
    if use_regime:
        mkt = (1 + sec.pct_change().fillna(0).mean(axis=1)).cumprod()
        ma_s = mkt.rolling(ma).mean()
        exp = (mkt > ma_s).astype(float).shift(1)
        exp[ma_s.isna()] = 1.0
        exp = exp.fillna(0.0)
        tgt = tgt.mul(exp, axis=0)
    return tgt


def main():
    sec = load_sectors()
    ret = sec.pct_change().fillna(0.0)
    print(f"sectors: {sec.shape[1]} x {len(sec)} days ({(1+ret.mean(axis=1)).cumprod().iloc[-1]:.1f}x EW growth)")

    # EW benchmark across sectors (with cost)
    n = sec.notna().sum(axis=1).replace(0, np.nan)
    ew = sec.notna().astype(float).div(n, axis=0).fillna(0)
    pr, eq = sim(ret, ew)
    m_ew = Metrics.from_returns(pr, eq)
    io = pr.index <= pd.Timestamp(IS_E)
    mo_ew = Metrics.from_returns(pr[~io], eq[~io])
    print(f"EW sectors bench: FULL Sharpe={m_ew.sharpe:.2f} CAGR={m_ew.cagr:.2f} | "
          f"OOS Sharpe={mo_ew.sharpe:.2f} MDD={mo_ew.max_dd:.2f}")

    print("\n--- Sector rotation (momentum) ---")
    rows = []
    for lb, k, reg in itertools.product([20, 60, 120], [2, 3, 5], [True, False]):
        tgt = sector_rotation(sec, lb, 20, k, reg, 100).shift(1).fillna(0.0)
        pr, eq = sim(ret, tgt)
        io = pr.index <= pd.Timestamp(IS_E)
        mi = Metrics.from_returns(pr[io], eq[io])
        mo = Metrics.from_returns(pr[~io], eq[~io])
        rows.append(dict(lb=lb, k=k, reg=reg, is_sh=mi.sharpe, oos_sh=mo.sharpe,
                         oos_cagr=mo.cagr, oos_mdd=mo.max_dd))
        print(f"  lb={lb:3d} k={k} regime={reg}: IS sh={mi.sharpe:.2f} | "
              f"OOS sh={mo.sharpe:.2f} CAGR={mo.cagr:.2f} MDD={mo.max_dd:.2f}")
    df = pd.DataFrame(rows).sort_values("oos_sh", ascending=False)
    print("\nBest sector-rotation configs (OOS Sharpe; EW bench=%.2f):" % mo_ew.sharpe)
    print(df.head(6).to_string(index=False))


if __name__ == "__main__":
    main()
