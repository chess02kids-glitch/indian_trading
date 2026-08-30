"""Benchmark comparison helpers (CAPM alpha/beta, information ratio)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def benchmark_returns(close_wide, rf=0.06):
    """Equal-weight market index return series (the investable universe)."""
    ret = close_wide.pct_change().fillna(0.0)
    ew = ret.mean(axis=1)
    return ew


def capm_alpha_beta(strat_ret: pd.Series, mkt_ret: pd.Series, rf=0.06):
    """Return (alpha_ann, beta, info_ratio) of strategy vs market."""
    s = strat_ret.subtract(rf / 252.0).dropna()
    m = mkt_ret.subtract(rf / 252.0).dropna()
    both = pd.concat([s, m], axis=1, join="inner").dropna()
    if len(both) < 30:
        return 0.0, 0.0, 0.0
    x = both.iloc[:, 1].values
    y = both.iloc[:, 0].values
    beta = np.cov(x, y)[0, 1] / (np.var(x) + 1e-12)
    alpha_d = y.mean() - beta * x.mean()
    alpha_ann = alpha_d * 252
    resid = y - beta * x
    te = resid.std(ddof=1) * np.sqrt(252)
    ir = alpha_d * 252 / te if te > 0 else 0.0
    return alpha_ann, beta, ir
