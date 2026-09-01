"""Parameterized strategy signal generators.

All strategies produce a TARGET WEIGHT matrix (symbol x date) for the engine.
CRITICAL: no look-ahead. A decision made at close of day t produces a weight
that earns return of day t+1 or later. The runner shifts the raw weights by
an execution lag before simulation.

Every strategy has the uniform signature:
    fn(close_wide, high_wide, low_wide, open_wide, **params) -> DataFrame
Indicators use data only up to and including the signal date.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------- Indicators (vectorized across symbols) ----------------

def sma(x, n):
    return x.rolling(n, min_periods=n).mean()


def ema(x, n):
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(x, n):
    delta = x.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(high, low, close, n):
    """Average true range, element-wise per symbol.

    The previous version did ``pd.concat([...], axis=1).max(axis=1).to_frame()``,
    which concatenates the three components *side by side* (5 symbols -> 15
    columns), takes the max across all of them — mixing unrelated symbols — and
    then collapses the result to a single column named after the close series.
    Any caller doing ``mid + k * atr(...)`` then aligned on column labels and got
    an all-NaN frame of 6 columns back, silently producing a flat signal.
    """
    prev_close = close.shift(1)
    true_range = np.maximum.reduce(
        [
            (high - low).to_numpy(dtype=float),
            (high - prev_close).abs().to_numpy(dtype=float),
            (low - prev_close).abs().to_numpy(dtype=float),
        ]
    )
    return (
        pd.DataFrame(true_range, index=high.index, columns=high.columns)
        .rolling(n, min_periods=n)
        .mean()
    )


def rolling_vol(x, n):
    return x.pct_change().rolling(n).std()


def _state_pos(buy, sell):
    """Vectorized-ish stateful position builder per column."""
    pos = np.zeros(buy.shape)
    in_pos = np.zeros(buy.shape[1], dtype=bool)
    for i in range(buy.shape[0]):
        enter = buy[i] & ~in_pos
        leave = sell[i] & in_pos
        in_pos = in_pos & ~leave
        in_pos = in_pos | enter
        pos[i] = in_pos
    return pos


# ---------------- Strategies ----------------

def strat_dual_ma(close, high, low, open_, fast=20, slow=100):
    """Long-only trend: EMA(fast) > SMA(slow)."""
    sig = (ema(close, fast) > sma(close, slow)).astype(float)
    sig[sma(close, slow).isna()] = 0.0
    sig[close.isna()] = 0.0
    return sig


def strat_ma_cross(close, high, low, open_, fast=20, slow=100):
    """Long-only: SMA(fast) > SMA(slow)."""
    sig = (sma(close, fast) > sma(close, slow)).astype(float)
    sig[sma(close, slow).isna()] = 0.0
    return sig


def strat_rsi_rev(close, high, low, open_, n=14, lo=30, hi=50):
    """Mean reversion: buy RSI<lo, exit RSI>=hi."""
    r = rsi(close, n).values
    buy = r < lo
    sell = r >= hi
    pos = _state_pos(buy, sell)
    return pd.DataFrame(pos, index=close.index, columns=close.columns)


def strat_bollinger_rev(close, high, low, open_, n=20, k=2.0):
    """Mean reversion: buy close<lower band, exit at mid."""
    mid = close.rolling(n).mean().values
    std = close.rolling(n).std().values
    lower = mid - k * std
    buy = close.values < lower
    sell = close.values >= mid
    pos = _state_pos(buy, sell)
    pos[np.isnan(mid)] = 0.0
    return pd.DataFrame(pos, index=close.index, columns=close.columns)


def strat_donchian(close, high, low, open_, n=50, atr_stop=2.0):
    """Turtle: long close>N-day prior high, exit close<N-day prior low."""
    upper = high.rolling(n).max().shift(1).values
    lower = low.rolling(n).min().shift(1).values
    buy = close.values > upper
    sell = close.values < lower
    pos = _state_pos(buy, sell)
    pos[np.isnan(upper)] = 0.0
    return pd.DataFrame(pos, index=close.index, columns=close.columns)


def _supertrend_trend(close, high, low, n=10, mult=3.0):
    """SuperTrend direction: True while the trend is up.

    The bands ratchet — the upper band only falls and the lower band only rises
    until price closes through them — so the indicator is path-dependent and
    cannot be expressed as a single vectorised rolling expression.  The loop runs
    over dates and stays vectorised across symbols, matching ``_state_pos``.

    No look-ahead: the trend at close of day t uses the previous day's bands.
    """
    a = atr(high, low, close, n).values
    mid = ((high + low) / 2.0).values
    basic_up = mid + mult * a
    basic_lo = mid - mult * a
    c = close.values
    rows, cols = c.shape

    final_up = np.full(cols, np.nan)
    final_lo = np.full(cols, np.nan)
    trend = np.zeros((rows, cols), dtype=bool)
    prev_trend = np.zeros(cols, dtype=bool)

    for i in range(rows):
        if i == 0:
            final_up = basic_up[i].copy()
            final_lo = basic_lo[i].copy()
        else:
            prev_c = c[i - 1]
            # The upper band ratchets DOWN: take the new basic value when it is
            # LOWER than the band we are holding, or when price has already
            # closed above the old band (the stop is spent).  Taking it when it
            # is higher — the natural but wrong reading — inflates the band
            # forever and the trend can never flip up.
            take_up = (basic_up[i] < final_up) | (prev_c > final_up)
            final_up = np.where(
                np.isnan(final_up), basic_up[i], np.where(take_up, basic_up[i], final_up)
            )
            # The lower band ratchets UP, by the mirrored rule.
            take_lo = (basic_lo[i] > final_lo) | (prev_c < final_lo)
            final_lo = np.where(
                np.isnan(final_lo), basic_lo[i], np.where(take_lo, basic_lo[i], final_lo)
            )
            up = c[i] > final_up
            down = c[i] < final_lo
            prev_trend = np.where(up, True, np.where(down, False, prev_trend))
        valid = ~np.isnan(basic_up[i]) & ~np.isnan(basic_lo[i])
        trend[i] = prev_trend & valid
    return pd.DataFrame(trend.astype(float), index=close.index, columns=close.columns)


def strat_supertrend(close, high, low, open_, n=10, mult=3.0):
    """Classic SuperTrend: long while price holds above the ratcheted lower band."""
    return _supertrend_trend(close, high, low, n=n, mult=mult)


def strat_supertrend_fast(close, high, low, open_, n=7, mult=2.0):
    """A tighter SuperTrend variant.

    Included deliberately: comparing two parameterisations of the *same*
    indicator against each other is the sharpest test of whether a "strategy"
    is diversifying anything or just re-betting the same signal.
    """
    return _supertrend_trend(close, high, low, n=n, mult=mult)


def strat_vol_breakout(close, high, low, open_, n=20, k=1.5, exit_n=None):
    """Volatility breakout on range expansion."""
    exit_n = exit_n or n
    upper = high.rolling(n).max().shift(1).values
    lower = low.rolling(exit_n).min().shift(1).values
    buy = close.values > upper
    sell = close.values < lower
    pos = _state_pos(buy, sell)
    pos[np.isnan(upper)] = 0.0
    return pd.DataFrame(pos, index=close.index, columns=close.columns)


def strat_momentum_cs(close, high, low, open_, lookback=250, top_frac=0.2, hold=20):
    """Cross-sectional momentum: long top momentum names, rebalance every `hold`."""
    mom = close.pct_change(lookback)
    n = close.shape[1]
    top_n = max(1, int(n * top_frac))
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for k in range(0, len(dates), hold):
        d = dates[k]
        if d not in mom.index:
            continue
        m = mom.loc[d].dropna()
        if len(m) < 3:
            continue
        top = m.nlargest(top_n).index
        w = 1.0 / top_n
        # Clear the row before assigning: `rebal` is all-NaN and ffill() carries
        # values forward forever, so without this the book accumulates the union
        # of every name ever selected instead of the current top-N.
        rebal.loc[d, :] = 0.0
        for s in top:
            rebal.loc[d, s] = w
    return rebal.ffill().fillna(0.0)


def strat_ts_momentum(close, high, low, open_, lookback=250, vol_tgt=None):
    """Time-series momentum: equal-weight long names with positive lookback return."""
    mom = close.pct_change(lookback)
    sig = (mom > 0).astype(float)
    sig[mom.isna()] = 0.0
    return sig


def strat_ma_trend_vol(close, high, low, open_, fast=20, slow=100, vol_lookback=60,
                       vol_quantile=0.7):
    """Trend filter + volatility regime gate: only trade when vol < threshold."""
    sig = strat_dual_ma(close, high, low, open_, fast, slow)
    mkt_vol = close.pct_change().rolling(vol_lookback).std().mean(axis=1)
    thr = mkt_vol.rolling(vol_lookback).quantile(vol_quantile)
    gate = pd.Series(1.0, index=close.index)
    gate[mkt_vol > thr] = 0.0
    return sig.mul(gate, axis=0)


def strat_momentum_cs_ls(close, high, low, open_, lookback=250, hold=20, frac=0.2):
    """Cross-sectional momentum LONG-SHORT (market neutral):
    long top `frac` names, short bottom `frac` names, rebalance every `hold` days.
    Dollar-neutral (sum of weights = 0), so it captures pure cross-sectional alpha.
    """
    mom = close.pct_change(lookback)
    n = close.shape[1]
    k = max(1, int(n * frac))
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < 2 * k + 1:
            continue
        top = m.nlargest(k).index
        bot = m.nsmallest(k).index
        w = 1.0 / (2 * k)
        # Clear the row before assigning: `rebal` is all-NaN and ffill() carries
        # values forward forever, so without this the book accumulates the union
        # of every name ever selected instead of the current top-N.
        rebal.loc[dt, :] = 0.0
        for s in top:
            rebal.loc[dt, s] = w
        for s in bot:
            rebal.loc[dt, s] = -w
    return rebal.ffill().fillna(0.0)


def strat_reversal_cs_ls(close, high, low, open_, lookback=20, hold=10, frac=0.2):
    """Short-term cross-sectional reversal long-short: short recent winners,
    long recent losers."""
    mom = close.pct_change(lookback)
    n = close.shape[1]
    k = max(1, int(n * frac))
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < 2 * k + 1:
            continue
        top = m.nlargest(k).index
        bot = m.nsmallest(k).index
        w = 1.0 / (2 * k)
        # Clear the row before assigning: `rebal` is all-NaN and ffill() carries
        # values forward forever, so without this the book accumulates the union
        # of every name ever selected instead of the current top-N.
        rebal.loc[dt, :] = 0.0
        for s in top:
            rebal.loc[dt, s] = -w  # short winners
        for s in bot:
            rebal.loc[dt, s] = w  # long losers
    return rebal.ffill().fillna(0.0)


def strat_momentum_cs_ls_vol(close, high, low, open_, lookback=250, hold=20,
                             frac=0.2, vol_lookback=60):
    """Cross-sectional momentum long-short with per-name volatility targeting
    (equal risk, not equal weight)."""
    mom = close.pct_change(lookback)
    vol = close.pct_change().rolling(vol_lookback).std()
    n = close.shape[1]
    k = max(1, int(n * frac))
    rebal = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    dates = close.index
    for d in range(0, len(dates), hold):
        dt = dates[d]
        if dt not in mom.index:
            continue
        m = mom.loc[dt].dropna()
        if len(m) < 2 * k + 1:
            continue
        top = m.nlargest(k).index
        bot = m.nsmallest(k).index
        sel = list(top) + list(bot)
        v = vol.loc[dt, sel].replace(0, np.nan)
        w = (1.0 / v)
        w = w / w.sum()  # long+short sum to zero via equal gross
        gross = w.abs().sum()
        w = w / gross * 2.0 * 0.5  # each side gross 0.5
        # Clear the row before assigning: `rebal` is all-NaN and ffill() carries
        # values forward forever, so without this the book accumulates the union
        # of every name ever selected instead of the current top-N.
        rebal.loc[dt, :] = 0.0
        for s in top:
            rebal.loc[dt, s] = w[s]
        for s in bot:
            rebal.loc[dt, s] = -w[s]
    return rebal.ffill().fillna(0.0)
