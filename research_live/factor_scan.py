"""Clean cross-sectional factor scan with proper 1-day lag and costs.

Holds the top `top_n` names by factor each month (equal weight, 1-day lag),
long-only. Reports IS/OOS Sharpe vs the EW benchmark. Uses the SAME engine
as momentum_study so results are directly comparable.
"""
from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.runner import StrategyRunner
from research_live.metrics import Metrics
from research_live.alpha import benchmark_returns

COST = 0.0015
IS_S, IS_E = "2009-01-01", "2019-12-31"
OOS_S, OOS_E = "2020-01-01", "2026-06-30"


def factor_select(close, factor, hold=20, top_n=15, lag_shift=True):
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in factor.index:
            continue
        m = factor.loc[dt].dropna()
        if len(m) < top_n:
            continue
        top = m.nlargest(top_n).index
        for s in top:
            rebal.loc[dt, s] = 1.0 / top_n
    return rebal.ffill().fillna(0.0)


def build_factor(close, kind, p):
    if kind == "momentum":
        return close.pct_change(p)
    if kind == "reversal":
        return -close.pct_change(p)
    if kind == "lowvol":
        vol = close.pct_change().rolling(p).std()
        return -vol
    if kind == "yearret":
        return close.pct_change(252)
    raise ValueError(kind)


def main():
    panel = load_panel()
    syms = liquid_universe(panel, "2008-01-01", 0.9)
    sub, close = align_panel(panel, syms, "2009-01-01", "2026-06-30")
    runner = StrategyRunner(sub, close, sub["high"].unstack("symbol"),
                            sub["low"].unstack("symbol"), sub["open"].unstack("symbol"),
                            cost_oneway=COST)
    mkt = benchmark_returns(close)
    m_ew = Metrics.from_returns(mkt, (1 + mkt).cumprod())
    print(f"EW benchmark: Sharpe={m_ew.sharpe:.2f} CAGR={m_ew.cagr:.2f} MDD={m_ew.max_dd:.2f}\n")

    rows = []
    for kind, params in [("momentum", [20, 60, 120, 250]),
                         ("reversal", [5, 10, 20]),
                         ("lowvol", [60, 120, 250]),
                         ("yearret", [252])]:
        for p in params:
            f = build_factor(close, kind, p)
            # rebalance monthly, top 15
            def strat(close, high, low, open_):
                return factor_select(close, f, hold=20, top_n=15)
            m_is, _, _ = runner.run_period(strat, IS_S, IS_E)
            m_oos, eq, ret = runner.run_period(strat, OOS_S, OOS_E)
            rows.append(dict(kind=kind, p=p, is_sh=m_is.sharpe, oos_sh=m_oos.sharpe,
                             oos_cagr=m_oos.cagr, oos_mdd=m_oos.max_dd))
            print(f"{kind:9s} p={p:4d} | IS sh={m_is.sharpe:.2f} | OOS sh={m_oos.sharpe:.2f} "
                  f"cagr={m_oos.cagr:.2f} mdd={m_oos.max_dd:.2f}")

    df = pd.DataFrame(rows).sort_values("oos_sh", ascending=False)
    df.to_csv("research_live/factor_scan.csv", index=False)
    print("\nSorted by OOS Sharpe (EW bench=%.2f):" % m_ew.sharpe)
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
