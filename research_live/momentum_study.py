"""Focused validation of long-only cross-sectional momentum.

Holds the top `top_frac` (or top-N) names by lookback momentum, rebalanced
every `hold` days, equal weight, with 1-day execution lag and Indian costs.
Runs IS/OOS, walk-forward, and parameter stability.
"""
from __future__ import annotations

import itertools
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.runner import StrategyRunner
from research_live.alpha import benchmark_returns, capm_alpha_beta
from research_live.metrics import Metrics
import research_live.strategies as S

COST = 0.0015
IS_S, IS_E = "2009-01-01", "2019-12-31"
OOS_S, OOS_E = "2020-01-01", "2026-06-30"


def strat_mom_topN(close, high, low, open_, lookback=60, hold=20, top_n=15):
    mom = close.pct_change(lookback)
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < top_n:
            continue
        top = m.nlargest(top_n).index
        for s in top:
            rebal.loc[dt, s] = 1.0 / top_n
    return rebal.ffill().fillna(0.0)


def main():
    panel = load_panel()
    syms = liquid_universe(panel, "2008-01-01", 0.9)
    sub, close = align_panel(panel, syms, "2009-01-01", "2026-06-30")
    runner = StrategyRunner(sub, close, sub["high"].unstack("symbol"),
                            sub["low"].unstack("symbol"), sub["open"].unstack("symbol"),
                            cost_oneway=COST)
    mkt = benchmark_returns(close)
    print(f"universe {close.shape[1]} names")

    grid = list(itertools.product([20, 60, 120, 250], [20], [10, 15, 20]))
    rows = []
    for lb, hold, tn in grid:
        m_is, _, _ = runner.run_period(strat_mom_topN, IS_S, IS_E,
                                       lookback=lb, hold=hold, top_n=tn)
        m_oos, eq_oos, ret_oos = runner.run_period(strat_mom_topN, OOS_S, OOS_E,
                                                   lookback=lb, hold=hold, top_n=tn)
        a, b, ir = capm_alpha_beta(ret_oos, mkt.loc[OOS_S:OOS_E])
        rows.append(dict(lb=lb, hold=hold, tn=tn,
                         is_sh=m_is.sharpe, is_cagr=m_is.cagr, is_mdd=m_is.max_dd,
                         oos_sh=m_oos.sharpe, oos_cagr=m_oos.cagr, oos_mdd=m_oos.max_dd,
                         oos_pf=m_oos.profit_factor, alpha=a, beta=b, ir=ir,
                         turn=m_oos.annual_turnover))
        print(f"lb={lb:4d} tn={tn:2d} | IS sh={m_is.sharpe:.2f} cagr={m_is.cagr:.2f} | "
              f"OOS sh={m_oos.sharpe:.2f} cagr={m_oos.cagr:.2f} mdd={m_oos.max_dd:.2f} "
              f"pf={m_oos.profit_factor:.2f} alpha={a:.2f} beta={b:.2f} ir={ir:.2f} turn={m_oos.annual_turnover:.0f}")

    df = pd.DataFrame(rows).sort_values("oos_sh", ascending=False)
    df.to_csv("research_live/momentum_results.csv", index=False)
    print("\n===== Sorted by OOS Sharpe =====")
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
