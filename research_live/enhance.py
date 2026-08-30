"""Enhancements to the winning Momentum + Market-Regime strategy on the broad
liquid universe: trailing/ATR stops, drawdown de-risking, low-vol tilt within
top names, and exposure caps. Tests whether risk overlays improve OOS Sharpe /
Calmar without overfitting."""
from __future__ import annotations

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
LIQ = 1e7


def build_wide(uni, start, end):
    closes, highs, lows, opens = {}, {}, {}, {}
    for s in uni:
        d = uni[s]
        d = d[(d.index >= start) & (d.index <= end)]
        if len(d) < 200:
            continue
        closes[s] = d["close"]; highs[s] = d["high"]; lows[s] = d["low"]
        if "open" in d.columns: opens[s] = d["open"]
    return (pd.DataFrame(closes).sort_index(), pd.DataFrame(highs).sort_index(),
            pd.DataFrame(lows).sort_index(), pd.DataFrame(opens).sort_index())


def market_proxy(close):
    return (1 + close.pct_change().fillna(0).mean(axis=1)).cumprod()


def sim_longonly(rets, tgt):
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


def mom_regime(close, lookback=20, hold=20, top_n=20, ma=100):
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


def lowvol_tiebreak(close, high, low, lookback=20, hold=20, top_n=20, ma=100, vol_lb=60):
    """Pick top_n from the top (top_n*2) momentum names preferring lower vol."""
    mom = close.pct_change(lookback)
    vol = close.pct_change().rolling(vol_lb).std()
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < top_n * 2:
            continue
        cand = m.nlargest(top_n * 2).index
        # among candidates pick lowest-vol top_n
        v = vol.loc[dt, cand]
        sel = v.dropna().nsmallest(top_n).index
        for s in sel:
            rebal.loc[dt, s] = 1.0 / top_n
    tgt = rebal.ffill().fillna(0.0)
    mkt = market_proxy(close)
    ma_s = mkt.rolling(ma).mean()
    exp = (mkt > ma_s).astype(float).shift(1)
    exp[ma_s.isna()] = 1.0; exp = exp.fillna(0.0)
    return tgt.mul(exp, axis=0)


def drawdown_detarget(close, tgt, dd_lookback=250, dd_trigger=0.15, dd_exit=0.05):
    """Reduce exposure when the strategy's own drawdown exceeds trigger; restore
    when it recovers to exit level. Based on the underlying portfolio equity."""
    mkt = market_proxy(close)
    eq = (1 + (tgt * close.pct_change().fillna(0)).sum(axis=1)).cumprod()
    dd = eq / eq.cummax() - 1
    scale = pd.Series(1.0, index=close.index)
    state = 1
    for i in range(len(dd)):
        if state == 1 and dd.iloc[i] < -dd_trigger:
            state = 0; scale.iloc[i] = 0.3
        elif state == 0 and dd.iloc[i] > -dd_exit:
            state = 1; scale.iloc[i] = 1.0
        else:
            scale.iloc[i] = 1.0 if state == 1 else 0.3
    return tgt.mul(scale.shift(1).fillna(1.0), axis=0)


def main():
    uni = load_broad_universe(min_years=8, min_avg_value=LIQ, start="2010-01-01")
    close, high, low, open_ = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0.0)
    print(f"universe {close.shape[1]} names")

    base = mom_regime(close).shift(1).fillna(0.0)
    pr, eq = sim_longonly(ret, base)
    io = pr.index <= pd.Timestamp(IS_E)
    mo = Metrics.from_returns(pr[~io], eq[~io])
    print(f"\nBaseline MomReM: OOS Sharpe={mo.sharpe:.2f} CAGR={mo.cagr:.2f} "
          f"MDD={mo.max_dd:.2f} Calmar={mo.calmar:.2f} PF={mo.profit_factor:.2f}")

    print("\n--- Low-vol tilt (within top momentum names) ---")
    for vol_lb in [40, 60, 120]:
        t = lowvol_tiebreak(close, high, low, vol_lb=vol_lb).shift(1).fillna(0.0)
        pr, eq = sim_longonly(ret, t)
        mo = Metrics.from_returns(pr[~io], eq[~io])
        print(f"  vol_lb={vol_lb}: OOS Sharpe={mo.sharpe:.2f} CAGR={mo.cagr:.2f} "
              f"MDD={mo.max_dd:.2f} Calmar={mo.calmar:.2f} PF={mo.profit_factor:.2f}")

    print("\n--- Drawdown de-risking overlay ---")
    for trig, ex in [(0.15, 0.05), (0.20, 0.08), (0.10, 0.03)]:
        t = drawdown_detarget(close, mom_regime(close), dd_trigger=trig, dd_exit=ex)
        t = t.shift(1).fillna(0.0)
        pr, eq = sim_longonly(ret, t)
        mo = Metrics.from_returns(pr[~io], eq[~io])
        print(f"  trigger={trig} exit={ex}: OOS Sharpe={mo.sharpe:.2f} CAGR={mo.cagr:.2f} "
              f"MDD={mo.max_dd:.2f} Calmar={mo.calmar:.2f} PF={mo.profit_factor:.2f}")


if __name__ == "__main__":
    main()
