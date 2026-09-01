"""Broad-universe momentum with a risk overlay (vol/regime/drawdown gate)
to reduce tail risk while preserving the momentum edge."""
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


def mom_tgt(close, lookback=20, hold=20, top_n=20, risk_kind=None, risk_p=None):
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
        # Clear the row first. `rebal` starts as all-NaN and ffill() carries
        # every value forward, so without this the book silently accumulates the
        # UNION of every name ever selected (~272 names held on average instead
        # of 20) and the backtest measures a near-equal-weight broad portfolio
        # rather than the top-N momentum book the strategy card describes.
        rebal.loc[dt, :] = 0.0
        for s in m.nlargest(top_n).index:
            rebal.loc[dt, s] = 1.0 / top_n
    tgt = rebal.ffill().fillna(0.0)

    # risk overlay (exposure scalar 0..1 applied to whole book)
    if risk_kind == "vol":
        rv = close.pct_change().mean(axis=1).rolling(risk_p).std() * np.sqrt(252)
        exp = (risk_p and 0.0)  # placeholder
        target_vol = 0.18
        exp = (target_vol / rv).clip(0.0, 1.0).shift(1).fillna(1.0)
        exp[rv > 0.30] = 0.0
    elif risk_kind == "momma":
        # price vs long MA of the market proxy
        mp = close.pct_change().mean(axis=1)
        mkt = (1 + mp).cumprod()
        ma = mkt.rolling(risk_p).mean()
        exp = (mkt > ma).astype(float).shift(1).fillna(0)
        # warmup
        exp[ma.isna()] = 1.0
    elif risk_kind == "drawdown":
        eq = (1 + close.pct_change().mean(axis=1).fillna(0)).cumprod()
        dd = eq / eq.cummax() - 1
        exp = (dd > -risk_p).astype(float).shift(1).fillna(1.0)
    else:
        exp = pd.Series(1.0, index=close.index)
    return tgt.mul(exp, axis=0)


def main():
    uni = load_broad_universe(min_years=8, min_avg_value=5e6, start="2010-01-01")
    close, high, low, open_ = build_wide(uni, S, E)
    ret = close.pct_change().fillna(0.0)
    print(f"universe {close.shape[1]} names")

    n = close.notna().sum(axis=1).replace(0, np.nan)
    ew_w = close.notna().astype(float).div(n, axis=0).fillna(0)
    m_ew = Metrics.from_returns((ew_w * ret).sum(axis=1), (1 + (ew_w * ret).sum(axis=1)).cumprod())
    print(f"Broad EW bench: Sharpe={m_ew.sharpe:.2f} CAGR={m_ew.cagr:.2f} MDD={m_ew.max_dd:.2f} Calmar={m_ew.calmar:.2f}")

    def sim(tgt):
        r = ret.reindex_like(tgt).fillna(0.0).values
        t = np.clip(tgt.fillna(0).values, 0, 1)
        g = t.sum(axis=1, keepdims=True)
        t = t * np.minimum(1.0 / np.maximum(g, 1e-12), 1.0)
        pr = (t * r).sum(axis=1)
        tn = np.vstack([t[1:], t[-1:]])
        w_end = t * (1 + r); w_end = w_end / np.maximum(1 + pr[:, None], 1e-12)
        turn = np.abs(tn - w_end).sum(axis=1)
        net = (1 + pr) - turn * COST
        return pd.Series(net - 1, index=ret.index), pd.Series(np.cumprod(net), index=ret.index)

    print("\n--- momentum + risk overlay (OOS) ---")
    best = None
    for lb, risk_kind, rp in [(20, None, None), (20, "vol", 20), (20, "vol", 60),
                              (20, "momma", 200), (20, "momma", 100),
                              (20, "drawdown", 0.15), (20, "drawdown", 0.20),
                              (60, None, None), (60, "vol", 40), (60, "momma", 200),
                              (60, "drawdown", 0.20)]:
        tgt = mom_tgt(close, lb, 20, 20, risk_kind, rp).shift(1).fillna(0.0)
        pr, eq = sim(tgt)
        idx = pr.index >= pd.Timestamp(OOS_S)
        m = Metrics.from_returns(pr[idx], eq[idx])
        tag = risk_kind or "none"
        print(f"mom lb={lb} overlay={tag}{rp or ''}: OOS Sharpe={m.sharpe:.2f} CAGR={m.cagr:.2f} "
              f"MDD={m.max_dd:.2f} Calmar={m.calmar:.2f} Sortino={m.sortino:.2f} PF={m.profit_factor:.2f}")
        if best is None or m.sharpe > best[1]:
            best = ((lb, tag, rp), m.sharpe, m)
    print("\nBEST OOS:", best[0], "sharpe", round(best[1], 2), "calmar", round(best[2].calmar, 2))


if __name__ == "__main__":
    main()
