"""Test whether volatility targeting / regime filters genuinely improve the
equal-weight benchmark on a risk-adjusted basis (lower drawdown, higher
Calmar/Sharpe) with realistic costs."""
from __future__ import annotations

import itertools
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/user/indian_trading")
from research_live.data import load_panel, liquid_universe, align_panel
from research_live.engine import simulate
from research_live.metrics import Metrics
from research_live.alpha import benchmark_returns

COST = 0.0015
IS_S, IS_E = "2009-01-01", "2019-12-31"
OOS_S, OOS_E = "2020-01-01", "2026-06-30"


def main():
    panel = load_panel()
    syms = liquid_universe(panel, "2008-01-01", 0.9)
    sub, close = align_panel(panel, syms, "2009-01-01", "2026-06-30")
    ret = close.pct_change().fillna(0.0)

    # Equal-weight daily-rebalanced portfolio
    ew = ret.mean(axis=1)
    eq = (1 + ew).cumprod()
    m_ew = Metrics.from_returns(ew, eq)
    print(f"EW benchmark: CAGR={m_ew.cagr:.2f} Sharpe={m_ew.sharpe:.2f} "
          f"Calmar={m_ew.calmar:.2f} MDD={m_ew.max_dd:.2f} vol={m_ew.vol:.2f}")

    # --- Volatility targeting: scale exposure so realized vol == target ---
    def vol_tgt_strat(target_vol, lookback, max_lev=1.0, lag=1):
        rv = ret.mean(axis=1).rolling(lookback).std().shift(lag) * np.sqrt(252)
        w = np.clip(target_vol / rv, 0.0, max_lev)
        w = w.fillna(1.0).clip(upper=max_lev)
        pr = ew * w
        # turnover of the scaling weight
        turn = (w - w.shift(1)).abs().fillna(0.0)
        daily_cost = turn * COST
        net = pr - daily_cost
        eqv = (1 + net).cumprod()
        return Metrics.from_returns(net, eqv), net

    print("\n--- Volatility targeting on EW ---")
    best = None
    for tv, lb in itertools.product([0.10, 0.13, 0.16, 0.20], [20, 40, 60, 120]):
        m_full, net = vol_tgt_strat(tv, lb)
        print(f"  tgt_vol={tv} lb={lb}: full Sharpe={m_full.sharpe:.2f} "
              f"CAGR={m_full.cagr:.2f} Calmar={m_full.calmar:.2f} MDD={m_full.max_dd:.2f}")
    return


if __name__ == "__main__":
    main()
