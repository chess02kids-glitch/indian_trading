"""Performance metrics for strategy evaluation."""
from __future__ import annotations

import numpy as np
import pandas as pd


class Metrics:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def to_dict(self):
        return dict(self.__dict__)

    @staticmethod
    def from_returns(rets: pd.Series, equity: pd.Series,
                     periods_per_year=252, rf=0.06, turnover=None) -> "Metrics":
        rets = rets.dropna()
        n = len(rets)
        if n < 2:
            return Metrics(**dict(cagr=0, sharpe=0, sortino=0, calmar=0,
                                  max_dd=0, total_ret=0, ann_ret=0, vol=0,
                                  n_days=n, years=0, profit_factor=0, win_rate=0,
                                  exposure=0, turnover=0, avg_daily_ret=0,
                                  downside_dev=0, recovery_factor=0))
        years = n / periods_per_year
        total_ret = equity.iloc[-1] / equity.iloc[0] - 1.0
        cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0
        ann_ret = total_ret / years
        vol = rets.std(ddof=1) * np.sqrt(periods_per_year)
        ann_rf = rf
        sharpe = (cagr - ann_rf) / vol if vol > 0 else 0.0
        dd = drawdown(equity)
        max_dd = float(dd.min())
        downside = rets[rets < 0]
        downside_dev = downside.std(ddof=1) * np.sqrt(periods_per_year) if len(downside) > 1 else 0.0
        sortino = (cagr - ann_rf) / downside_dev if downside_dev > 0 else 0.0
        calmar = cagr / abs(max_dd) if max_dd < 0 else 0.0
        recovery_factor = total_ret / abs(max_dd) if max_dd < 0 else 0.0
        # trade-like stats on daily returns (proxy)
        gains = rets[rets > 0]
        losses = rets[rets < 0]
        pf = (gains.sum()) / (abs(losses.sum())) if losses.sum() != 0 else float("inf")
        win_rate = (rets > 0).mean()
        exposure = (rets != 0).mean()
        avg_daily = rets.mean()
        m = dict(cagr=cagr, sharpe=sharpe, sortino=sortino, calmar=calmar,
                 max_dd=max_dd, total_ret=total_ret, ann_ret=ann_ret, vol=vol,
                 n_days=n, years=years, profit_factor=pf, win_rate=win_rate,
                 exposure=exposure, avg_daily_ret=avg_daily,
                 downside_dev=downside_dev, recovery_factor=recovery_factor,
                 turnover=float(turnover.mean()) if turnover is not None and len(turnover) else 0.0,
                 annual_turnover=float(turnover.mean()) * periods_per_year if turnover is not None and len(turnover) else 0.0)
        return Metrics(**m)


def drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def deflated_sharpe(sharpe, n_obs, n_trials, skew=0, kurt=3, sr=0.0):
    """PSR-style deflated Sharpe probability (approx)."""
    if n_obs < 2 or n_trials < 1:
        return 0.0
    # standard error under normality
    se = 1.0 / np.sqrt(n_obs - 1)
    sr0 = sharpe
    from scipy import stats
    # expected max SR under n_trials
    e_max = sr0 - np.sqrt(2 * np.log(n_trials)) * se  # rough
    t = (sr0 - e_max) / se
    return float(stats.norm.cdf(t))
