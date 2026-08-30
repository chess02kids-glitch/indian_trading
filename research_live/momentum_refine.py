"""Momentum-family refinements on the broad liquid universe (long-only):
multi-timeframe confirmation, price acceleration, volume confirmation,
weekly momentum. Tests whether any beats the MomReM baseline OOS."""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.broad_data import load_broad_universe
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2010-01-01", "2026-06-30"
OOS_S, OOS_E = "2019-01-01", "2026-06-30"
LIQ = 1e7


def build_wide(uni, start, end):
    closes, vols = {}, {}
    for s in uni:
        d = uni[s]
        d = d[(d.index >= start) & (d.index <= end)]
        if len(d) < 200:
            continue
        closes[s] = d["close"]
        if "volume" in d.columns:
            vols[s] = d["volume"]
    close = pd.DataFrame(closes).sort_index()
    vol = pd.DataFrame(vols).sort_index().reindex_like(close)
    return close, vol


def market_proxy(close):
    return (1 + close.pct_change().fillna(0).mean(axis=1)).cumprod()


def apply_regime(close, tgt, ma=100):
    mkt = market_proxy(close)
    ma_s = mkt.rolling(ma).mean()
    exp = (mkt > ma_s).astype(float).shift(1)
    exp[ma_s.isna()] = 1.0; exp = exp.fillna(0.0)
    return tgt.mul(exp, axis=0)


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


def mom_selector(close, score, hold=20, top_n=20, ma=100):
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        m = score.loc[dt].dropna()
        if len(m) < top_n + 3:
            continue
        for s in m.nlargest(top_n).index:
            rebal.loc[dt, s] = 1.0 / top_n
    tgt = rebal.ffill().fillna(0.0)
    return apply_regime(close, tgt, ma)


def main():
    uni = load_broad_universe(min_years=8, min_avg_value=LIQ, start="2010-01-01")
    close, vol = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0.0)
    print(f"universe {close.shape[1]} names")

    mom20 = close.pct_change(20)
    mom60 = close.pct_change(60)
    mom120 = close.pct_change(120)

    def eval_(name, score):
        tgt = mom_selector(close, score).shift(1).fillna(0.0)
        pr, eq = sim(ret, tgt)
        io = pr.index >= pd.Timestamp(OOS_S)
        mo = Metrics.from_returns(pr[io], eq[io])
        print(f"  {name:34s}: OOS Sharpe={mo.sharpe:.2f} CAGR={mo.cagr:.2f} "
              f"MDD={mo.max_dd:.2f} Calmar={mo.calmar:.2f} PF={mo.profit_factor:.2f}")

    print("--- Baseline ---")
    eval_("MomReM (20d momentum)", mom20)

    print("--- Multi-timeframe (composite scores) ---")
    eval_("20+60 avg momentum", (mom20 + mom60) / 2)
    eval_("20+60+120 avg momentum", (mom20 + mom60 + mom120) / 3)
    # require both positive
    eval_("20d (60d confirm filter)", mom20.where(mom60 > 0, np.nan))
    eval_("Price acceleration (20d-60d)", mom20 - mom60)

    print("--- Volume confirmation ---")
    # volume ratio (recent 20d avg vol / 120d avg vol) as tiebreak blend
    v20 = vol.rolling(20).mean()
    v120 = vol.rolling(120).mean()
    vol_ratio = v20 / v120
    eval_("20d mom + volume surge blend", mom20 * (0.7 + 0.3 * vol_ratio.clip(0.5, 2.0)))
    eval_("20d mom + vol-sorted (mom then low-vol)", mom20)

    print("--- Weekly momentum (hold 20 = ~monthly, lookback 100) ---")
    eval_("100d momentum (weekly-ish)", close.pct_change(100))
    eval_("20d + 100d momentum avg", (mom20 + close.pct_change(100)) / 2)


if __name__ == "__main__":
    main()
