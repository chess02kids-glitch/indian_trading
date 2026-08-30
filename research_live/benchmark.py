"""Realistic monthly-rebalanced equal-weight benchmark WITH transaction costs."""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.engine import simulate
from research_live.metrics import Metrics

COST = 0.0015
S, E = "2009-01-01", "2026-06-30"


def main():
    panel = load_panel()
    syms = liquid_universe(panel, "2008-01-01", 0.9)
    sub, close = align_panel(panel, syms, S, E)
    ret = close.pct_change().fillna(0.0)

    # Monthly-rebalanced EW with 1-day lag (decision end of month, trade next day)
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    for dt in close.index[::21]:
        n = close.loc[dt].notna().sum()
        if n == 0:
            continue
        rebal.loc[dt] = close.loc[dt].notna().astype(float) / n
    tgt = rebal.ffill().fillna(0.0)
    res = simulate(ret, tgt, cost_oneway=COST)
    m = res.metrics()
    print(f"Monthly-rebalanced EW (with costs): CAGR={m.cagr:.3f} Sharpe={m.sharpe:.2f} "
          f"Calmar={m.calmar:.2f} MDD={m.max_dd:.2f} vol={m.vol:.2f} "
          f"turnover/yr={m.annual_turnover:.0f}")

    # Buy & hold from 2009 (no rebalance, no cost) as lower bound
    bh = ret.iloc[0].fillna(0)  # not used
    # Buy-and-hold equal weight fixed at start weights
    w0 = close.iloc[0].notna().astype(float)
    w0 = w0 / w0.sum()
    tgt_bh = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    tgt_bh.loc[close.index[0]] = w0
    res_bh = simulate(ret, tgt_bh.ffill().fillna(0), cost_oneway=0.0)
    m_bh = res_bh.metrics()
    print(f"Buy&hold EW (no cost):          CAGR={m_bh.cagr:.3f} Sharpe={m_bh.sharpe:.2f} "
          f"Calmar={m_bh.calmar:.2f} MDD={m_bh.max_dd:.2f}")

    # Save benchmark equity for downstream
    eq = (1 + res.port_ret).cumprod()
    eq.to_csv("research_live/benchmark_ew.csv", header=["equity"])


if __name__ == "__main__":
    main()
