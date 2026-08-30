"""Experiment runner: builds target weights, applies execution lag,
simulates, splits train/test, and reports metrics.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import simulate
from .metrics import Metrics


class StrategyRunner:
    def __init__(self, panel, close_wide, high_wide, low_wide, open_wide,
                 cost_oneway=0.0015, exec_lag=1):
        self.panel = panel
        self.close = close_wide
        self.high = high_wide
        self.low = low_wide
        self.open = open_wide
        self.cost_oneway = cost_oneway
        self.exec_lag = exec_lag

    def signal(self, strat_fn, **kw):
        """Compute raw target weights (decision at close t)."""
        return strat_fn(self.close, self.high, self.low, self.open, **kw)

    def run(self, strat_fn, exec_lag=None, long_short=False, **kw):
        """Full backtest with execution lag."""
        lag = self.exec_lag if exec_lag is None else exec_lag
        raw = strat_fn(self.close, self.high, self.low, self.open, **kw)
        # execution lag: position decided at t is traded at t+lag, earns t+lag.. returns
        tgt = raw.shift(lag)
        tgt = tgt.fillna(0.0)
        ret = self.close.pct_change()
        res = simulate(ret, tgt, cost_oneway=self.cost_oneway, long_short=long_short)
        return res, raw

    def evaluate(self, strat_fn, **kw):
        res, raw = self.run(strat_fn, **kw)
        return res.metrics(), res

    def run_period(self, strat_fn, start, end, long_short=False, **kw):
        """Backtest restricted to [start, end]."""
        res, raw = self.run(strat_fn, long_short=long_short, **kw)
        idx = (res.equity.index >= pd.Timestamp(start)) & (res.equity.index <= pd.Timestamp(end))
        eq = res.equity[idx]
        ret = res.port_ret[idx]
        turn = res.turnover[idx]
        m = Metrics.from_returns(ret, eq, turnover=turn)
        return m, eq, ret


def walk_forward(runner, strat_fn, train_years=3, test_months=12, step_months=6,
                 params=None, start="2009-01-01", end="2026-06-30", **fixed):
    """Anchored walk-forward. Returns list of (test_start, test_end, Metrics)."""
    dates = pd.date_range(start, end, freq="MS")
    windows = []
    t0 = pd.Timestamp(start)
    test_len = pd.DateOffset(months=test_months)
    step = pd.DateOffset(months=step_months)
    cur = t0 + pd.DateOffset(years=train_years)
    results = []
    while cur <= pd.Timestamp(end):
        test_end = min(cur + test_len, pd.Timestamp(end))
        if test_end <= cur:
            break
        if params is not None:
            # choose params on train period (cur - train_years..cur)
            pass
        m, eq, ret = runner.run_period(strat_fn, cur, test_end, **(params or {}))
        results.append((cur, test_end, m))
        cur = cur + step
    return results
