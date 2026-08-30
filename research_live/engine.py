"""Vectorized portfolio simulation engine with realistic Indian costs.

Supports long-only (weights >= 0, gross capped at 1) and long-short
(weights can be negative, gross leveraged) portfolios.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .metrics import Metrics


def simulate(ret_wide: pd.DataFrame, target_wide: pd.DataFrame,
             cost_oneway: float = 0.0015, initial_cash: float = 1.0,
             long_short: bool = False, max_gross: float = 1.0):
    """Simulate a portfolio that rebalances to `target` weights each close.

    target_wide[s, t] = desired position in symbol s as a fraction of capital
    held THROUGH day t (earns return of day t). Callers shift signals by an
    execution lag.

    long_short=False: weights clipped to [0,1], gross scaled to <=1.
    long_short=True: weights kept as-is, scaled so gross == max_gross
    (default 1.0 => fully invested dollar-neutral if shorts balance longs).
    """
    r = ret_wide.reindex_like(target_wide).fillna(0.0).values
    tgt = target_wide.fillna(0.0).values

    if not long_short:
        tgt = np.clip(tgt, 0.0, 1.0)
        gross = tgt.sum(axis=1, keepdims=True)
        scale = np.minimum(1.0 / np.maximum(gross, 1e-12), 1.0)
        tgt = tgt * scale
    else:
        # scale each row so that sum of |w| == max_gross
        gross = np.abs(tgt).sum(axis=1, keepdims=True)
        tgt = tgt / np.maximum(gross, 1e-12) * max_gross

    T, N = tgt.shape
    pr = (tgt * r).sum(axis=1)

    with np.errstate(divide="ignore", invalid="ignore"):
        w_end = tgt * (1.0 + r)
    denom = 1.0 + pr[:, None]
    w_end = w_end / np.maximum(denom, 1e-12)

    tgt_next = np.vstack([tgt[1:], tgt[-1:]])
    turnover = np.abs(tgt_next - w_end).sum(axis=1)
    cost_rate = cost_oneway

    daily_cost = turnover * cost_rate
    gross_ret = 1.0 + pr
    net_ret = gross_ret - daily_cost
    equity = initial_cash * np.cumprod(net_ret)

    dates = ret_wide.index
    eq_series = pd.Series(equity, index=dates, name="equity")
    port_ret = pd.Series(net_ret - 1.0, index=dates, name="ret")
    turn = pd.Series(turnover, index=dates, name="turnover")
    return SimulationResult(eq_series, port_ret, turn, ret_wide.index, w_end)


class SimulationResult:
    def __init__(self, equity, port_ret, turnover, index, final_weights):
        self.equity = equity
        self.port_ret = port_ret
        self.turnover = turnover
        self.final_weights = final_weights
        self.index = index

    def metrics(self, rf: float = 0.06, periods_per_year: int = 252) -> Metrics:
        rets = self.port_ret.dropna()
        return Metrics.from_returns(rets, self.equity, periods_per_year, rf,
                                    self.turnover)
