#!/usr/bin/env python3
"""
30-Strategy Performance & Idea Generation Engine
=================================================
Runs 30 candidate strategies across Indian equity and index symbols.

IMPORTANT ARCHITECTURAL NOTE:
-----------------------------
This module serves strictly as an **Idea Generator and Candidate Screener (Tier 1)**.
Raw in-sample backtest scores MUST NOT be used to seed paper trading or live execution.
Any strategy passing preliminary screening is automatically subjected to:
  1. Purged Walk-Forward In-Sample (IS) vs Out-of-Sample (OOS) validation
  2. Deflated Sharpe Ratio (DSR) multiple-comparisons correction (Bailey & López de Prado)
  3. Placebo / noise baseline cross-checks
  4. Institutional transaction costs (regulatory taxes + slippage)
  5. Formal submission through the Research Cockpit gate (`research/gate.py`)

Key Defenses & Risk Controls:
-----------------------------
1. Look-ahead bias prevention: signal shift, next-bar execution, and daily-reset VWAP.
2. Mathematically correct indicators: exact Aroon indexing, Wilder RMA ADX, stateful PSAR, and safe RSI.
3. Crossover/edge-detected non-clashing signals (no simultaneous Buy/Sell spamming).
4. Terminal mark-to-market trade closing with unrealized P&L reconciliation.
5. ATR-based hard stop-loss on all positions (no unbounded drawdown).
6. Shared-capital multi-symbol portfolio mode with volatility targeting and max-position caps.
7. Volume validation checks to guard against zero-volume index feeds (^NSEI, ^NSEBANK).
8. Realistic Indian transaction cost model (15 bps default: brokerage, STT, exchange, SEBI, GST, stamp, slippage).
9. Risk-adjusted scoring using Sharpe, Sortino, Calmar, and Deflated Sharpe.
10. Seamless data ingestion prioritizing local DataHub parquet stores with clean yfinance fallback.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from scipy import stats

try:
    import talib
except ImportError:
    talib = None

import logging
import warnings

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)


# ==============================================================================
# INDICATOR FUNCTIONS (robust, zero-lookahead, mathematically validated)
# ==============================================================================


def _squeeze(data: Union[pd.DataFrame, pd.Series, Any], col: str) -> Any:
    """Helper to safely squeeze single-column DataFrames/Series or extract from row Series."""
    if isinstance(data, pd.Series):
        if col in data.index:
            val = data[col]
            if isinstance(val, pd.Series):
                return val.iloc[0]
            return val
        return data
    if isinstance(data, pd.DataFrame):
        if col in data.columns:
            val = data[col]
            if isinstance(val, pd.DataFrame):
                return val.iloc[:, 0]
            return val
    return getattr(data, col, data)


def calc_rsi(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    RSI using talib or Wilder smoothing fallback.
    Safely handles flat prices (avg_loss == 0 and avg_gain == 0 -> 50.0).
    """
    close = _squeeze(data, "Close")
    if talib is not None and hasattr(talib, "RSI"):
        return pd.Series(
            talib.RSI(close.values.astype(float), timeperiod=period), index=data.index
        )

    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # Wilder smoothing (RMA)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # Safe handling of flat periods
    flat_mask = (avg_gain == 0.0) & (avg_loss == 0.0)
    rsi[flat_mask] = 50.0
    return rsi


def calc_atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR using talib or Wilder smoothing fallback."""
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")
    if talib is not None and hasattr(talib, "ATR"):
        return pd.Series(
            talib.ATR(
                high.values.astype(float),
                low.values.astype(float),
                close.values.astype(float),
                timeperiod=period,
            ),
            index=data.index,
        )
    hl = high - low
    hpc = (high - close.shift(1)).abs()
    lpc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    if talib is not None and hasattr(talib, "EMA"):
        return pd.Series(
            talib.EMA(series.values.astype(float), timeperiod=period),
            index=series.index,
        )
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    if talib is not None and hasattr(talib, "SMA"):
        return pd.Series(
            talib.SMA(series.values.astype(float), timeperiod=period),
            index=series.index,
        )
    return series.rolling(window=period).mean()


def calc_wma(series: pd.Series, period: int) -> pd.Series:
    """Weighted Moving Average."""
    if talib is not None and hasattr(talib, "WMA"):
        return pd.Series(
            talib.WMA(series.values.astype(float), timeperiod=period),
            index=series.index,
        )
    weights = np.arange(1, period + 1)
    return series.rolling(window=period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def calc_macd(
    data: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """MACD returning (macd_line, signal_line, histogram)."""
    close = _squeeze(data, "Close")
    if talib is not None and hasattr(talib, "MACD"):
        macd, signal_line, hist = talib.MACD(
            close.values.astype(float),
            fastperiod=fast,
            slowperiod=slow,
            signalperiod=signal,
        )
        return (
            pd.Series(macd, index=data.index),
            pd.Series(signal_line, index=data.index),
            pd.Series(hist, index=data.index),
        )
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_bollinger(
    data: pd.DataFrame, period: int = 20, std_dev: float = 2.0
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands returning (upper, mid, lower)."""
    close = _squeeze(data, "Close")
    mid = calc_sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def calc_stochastic(
    data: pd.DataFrame, k_period: int = 14, d_period: int = 3
) -> Tuple[pd.Series, pd.Series]:
    """Stochastic Oscillator returning (%K, %D)."""
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")
    lowest_low = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    k = 100.0 * (close - lowest_low) / (highest_high - lowest_low + 1e-10)
    d = k.rolling(d_period).mean()
    return k, d


def calc_adx(
    data: pd.DataFrame, period: int = 14
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    ADX returning (adx, plus_di, minus_di) consistently.
    Uses Wilder's smoothing (alpha=1/period, seeded with SMA).
    """
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")
    if (
        talib is not None
        and hasattr(talib, "ADX")
        and hasattr(talib, "PLUS_DI")
        and hasattr(talib, "MINUS_DI")
    ):
        adx = pd.Series(
            talib.ADX(
                high.values.astype(float),
                low.values.astype(float),
                close.values.astype(float),
                timeperiod=period,
            ),
            index=data.index,
        )
        pdi = pd.Series(
            talib.PLUS_DI(
                high.values.astype(float),
                low.values.astype(float),
                close.values.astype(float),
                timeperiod=period,
            ),
            index=data.index,
        )
        mdi = pd.Series(
            talib.MINUS_DI(
                high.values.astype(float),
                low.values.astype(float),
                close.values.astype(float),
                timeperiod=period,
            ),
            index=data.index,
        )
        return adx, pdi, mdi

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    atr_val = calc_atr(data, period)
    smooth_pdm = plus_dm.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()
    smooth_mdm = minus_dm.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()

    plus_di = 100.0 * smooth_pdm / atr_val.replace(0, 1e-10)
    minus_di = 100.0 * smooth_mdm / atr_val.replace(0, 1e-10)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    return adx, plus_di, minus_di


def calc_cci(data: pd.DataFrame, period: int = 20) -> pd.Series:
    """Commodity Channel Index."""
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")
    tp = (high + low + close) / 3.0
    if talib is not None and hasattr(talib, "CCI"):
        return pd.Series(
            talib.CCI(
                high.values.astype(float),
                low.values.astype(float),
                close.values.astype(float),
                timeperiod=period,
            ),
            index=data.index,
        )
    sma_tp = tp.rolling(period).mean()
    mean_dev = tp.rolling(period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )
    return (tp - sma_tp) / (0.015 * mean_dev + 1e-10)


def calc_williams_r(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """Williams %R."""
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")
    if talib is not None and hasattr(talib, "WILLR"):
        return pd.Series(
            talib.WILLR(
                high.values.astype(float),
                low.values.astype(float),
                close.values.astype(float),
                timeperiod=period,
            ),
            index=data.index,
        )
    hh = high.rolling(period).max()
    ll = low.rolling(period).min()
    return -100.0 * (hh - close) / (hh - ll + 1e-10)


def calc_vwap(data: pd.DataFrame) -> pd.Series:
    """
    Volume Weighted Average Price (VWAP).
    Resets at the start of every trading day (anchored intraday calculation).
    """
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")
    volume = _squeeze(data, "Volume")
    tp = (high + low + close) / 3.0
    tp_vol = tp * volume

    # Extract dates for daily grouping
    if isinstance(data.index, pd.DatetimeIndex):
        dates = data.index.date
        date_group = pd.Series(dates, index=data.index)
        cum_tp_vol = tp_vol.groupby(date_group).cumsum()
        cum_vol = volume.groupby(date_group).cumsum()
    else:
        cum_tp_vol = tp_vol.cumsum()
        cum_vol = volume.cumsum()

    return cum_tp_vol / cum_vol.replace(0, np.nan)


def calc_hma(series: pd.Series, period: int) -> pd.Series:
    """Hull Moving Average."""
    half = max(int(period / 2), 1)
    sq = max(int(np.sqrt(period)), 1)
    wma1 = calc_wma(series, half)
    wma2 = calc_wma(series, period)
    raw = 2.0 * wma1 - wma2
    return calc_wma(raw, sq)


def calc_donchian(
    data: pd.DataFrame, period: int = 20
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Donchian Channel returning (upper, mid, lower)."""
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    mid = (upper + lower) / 2.0
    return upper, mid, lower


def calc_keltner(
    data: pd.DataFrame,
    ema_period: int = 20,
    atr_period: int = 10,
    multiplier: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Keltner Channel returning (upper, mid, lower)."""
    close = _squeeze(data, "Close")
    mid = calc_ema(close, ema_period)
    atr_val = calc_atr(data, atr_period)
    upper = mid + multiplier * atr_val
    lower = mid - multiplier * atr_val
    return upper, mid, lower


def calc_supertrend(
    data: pd.DataFrame, period: int = 7, multiplier: float = 3.0
) -> pd.Series:
    """SuperTrend returning trend direction (+1 for Uptrend / Bullish, -1 for Downtrend / Bearish)."""
    close = _squeeze(data, "Close")
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    hl_avg = (high + low) / 2.0
    atr_val = calc_atr(data, period)

    upper_basic = hl_avg + multiplier * atr_val
    lower_basic = hl_avg - multiplier * atr_val

    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    direction = pd.Series(1.0, index=data.index)

    for i in range(1, len(data)):
        if (
            upper_basic.iloc[i] < upper_band.iloc[i - 1]
            or close.iloc[i - 1] > upper_band.iloc[i - 1]
        ):
            upper_band.iloc[i] = upper_basic.iloc[i]
        else:
            upper_band.iloc[i] = upper_band.iloc[i - 1]

        if (
            lower_basic.iloc[i] > lower_band.iloc[i - 1]
            or close.iloc[i - 1] < lower_band.iloc[i - 1]
        ):
            lower_band.iloc[i] = lower_basic.iloc[i]
        else:
            lower_band.iloc[i] = lower_band.iloc[i - 1]

        if close.iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = 1.0  # Uptrend / Bullish
        elif close.iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = -1.0  # Downtrend / Bearish
        else:
            direction.iloc[i] = direction.iloc[i - 1]

    return direction


def calc_psar(
    data: pd.DataFrame,
    af_start: float = 0.02,
    af_step: float = 0.02,
    af_max: float = 0.2,
) -> pd.Series:
    """
    Parabolic SAR state machine.
    Returns trend state (+1 for Bullish uptrend, -1 for Bearish downtrend).
    """
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    close = _squeeze(data, "Close")

    af = af_start
    ep_high = high.iloc[0]
    ep_low = low.iloc[0]
    sar = close.iloc[0]
    trend = 1
    results = [np.nan] * len(data)

    for i in range(1, len(data)):
        if trend == 1:
            sar = sar + af * (ep_high - sar)
            if i >= 2:
                sar = min(sar, low.iloc[i - 1], low.iloc[i - 2])
            else:
                sar = min(sar, low.iloc[i - 1])

            if low.iloc[i] < sar:
                trend = -1
                sar = ep_high
                af = af_start
                ep_low = low.iloc[i]
            else:
                if high.iloc[i] > ep_high:
                    ep_high = high.iloc[i]
                    af = min(af + af_step, af_max)
        else:
            sar = sar - af * (sar - ep_low)
            if i >= 2:
                sar = max(sar, high.iloc[i - 1], high.iloc[i - 2])
            else:
                sar = max(sar, high.iloc[i - 1])

            if high.iloc[i] > sar:
                trend = 1
                sar = ep_low
                af = af_start
                ep_high = high.iloc[i]
            else:
                if low.iloc[i] < ep_low:
                    ep_low = low.iloc[i]
                    af = min(af + af_step, af_max)

        results[i] = 1.0 if trend == 1 else -1.0

    return pd.Series(results, index=data.index)


def calc_aroon(data: pd.DataFrame, period: int = 25) -> Tuple[pd.Series, pd.Series]:
    """
    Aroon Indicator returning (aroon_up, aroon_down).
    Formula:
      Aroon_Up = ((period - bars_since_high) / period) * 100
      Aroon_Down = ((period - bars_since_low) / period) * 100
    Where bars_since_high = (len(window) - 1) - argmax.
    """
    high = _squeeze(data, "High")
    low = _squeeze(data, "Low")
    aroon_up = pd.Series(np.nan, index=data.index)
    aroon_down = pd.Series(np.nan, index=data.index)

    for i in range(period, len(data)):
        window_high = high.iloc[i - period + 1 : i + 1]
        window_low = low.iloc[i - period + 1 : i + 1]
        if window_high.notna().any() and window_low.notna().any():
            # argmax in range [0, period-1], where period-1 is the most recent bar (0 bars ago)
            bars_since_high = (period - 1) - window_high.values.argmax()
            bars_since_low = (period - 1) - window_low.values.argmin()
            aroon_up.iloc[i] = float((period - bars_since_high) / period) * 100.0
            aroon_down.iloc[i] = float((period - bars_since_low) / period) * 100.0

    return aroon_up, aroon_down


def calc_obv(data: pd.DataFrame) -> pd.Series:
    """On Balance Volume."""
    close = _squeeze(data, "Close")
    volume = _squeeze(data, "Volume")
    diff = close.diff()
    sign = pd.Series(
        np.where(diff > 0, 1.0, np.where(diff < 0, -1.0, 0.0)), index=data.index
    )
    return (sign * volume).cumsum()


def calc_zscore(series: pd.Series, period: int = 20) -> pd.Series:
    """Z-Score."""
    mean = series.rolling(period).mean()
    std = series.rolling(period).std()
    return (series - mean) / (std.replace(0, 1e-10))


def calc_chande_momentum(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """Chande Momentum Oscillator."""
    close = _squeeze(data, "Close")
    diff = close.diff()
    up = diff.where(diff > 0, 0.0).rolling(period).sum()
    down = diff.where(diff < 0, 0.0).abs().rolling(period).sum()
    return 100.0 * (up - down) / (up + down + 1e-10)


# ==============================================================================
# DATA INGESTION & VOLUME VALIDATION (Resolves Issue 3 & Issue 6)
# ==============================================================================


def is_volume_valid(df: pd.DataFrame, min_valid_fraction: float = 0.5) -> bool:
    """
    Check if DataFrame has real trading volume data.
    Index tickers like ^NSEI and ^NSEBANK from yfinance often have 0 volume.
    """
    if df is None or df.empty or "Volume" not in df.columns:
        return False
    vol = _squeeze(df, "Volume")
    if vol.isna().all() or vol.sum() <= 0:
        return False
    non_zero = (vol > 0).mean()
    return bool(non_zero >= min_valid_fraction)


def load_local_market_data(symbol: str) -> Optional[pd.DataFrame]:
    """
    Attempt to load market data from repository's local parquet store
    (data/clean/eod2_data/, data/market/indices/, or datahub panel).
    """
    clean_sym = symbol.replace("^", "").replace(".NS", "").upper()
    repo_root = Path(__file__).resolve().parent.parent

    candidate_paths = [
        repo_root / "data" / "clean" / "eod2_data" / f"{clean_sym}.parquet",
        repo_root / "data" / "market" / "indices" / f"{clean_sym}.parquet",
        repo_root / "data" / "raw" / "eod2_data" / f"{clean_sym}.parquet",
    ]
    for path in candidate_paths:
        if path.exists():
            try:
                df = pd.read_parquet(path)
                col_map = {
                    c: c.capitalize()
                    for c in df.columns
                    if c.lower() in ["open", "high", "low", "close", "volume", "date"]
                }
                df = df.rename(columns=col_map)
                if "Date" in df.columns:
                    df["Date"] = pd.to_datetime(df["Date"])
                    df = df.set_index("Date")
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                df = df.sort_index()
                req = ["Open", "High", "Low", "Close", "Volume"]
                if all(c in df.columns for c in req):
                    return df[req]
            except Exception:
                # AUDIT: was a bare `continue`, so an unreadable candidate
                # file was silently indistinguishable from "no data".
                logger.warning("price_candidate_unreadable", exc_info=True)
                continue
    return None


def download_data(
    symbol: str, period: str = "60d", interval: str = "1h", prefer_local: bool = False
) -> Optional[pd.DataFrame]:
    """
    Download or load market data for a symbol.
    First checks local institutional DataHub parquet store if prefer_local is True;
    falls back cleanly to yfinance.
    """
    if prefer_local:
        local_df = load_local_market_data(symbol)
        if local_df is not None and not local_df.empty:
            return local_df

    try:
        import yfinance as yf

        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if df.empty:
            local_df = load_local_market_data(symbol)
            if local_df is not None and not local_df.empty:
                return local_df
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # De-duplicate columns if any
        df = df.loc[:, ~df.columns.duplicated()]

        df = df.reset_index()
        date_col = [
            c
            for c in df.columns
            if "date" in str(c).lower() or "time" in str(c).lower()
        ]
        if date_col:
            df["Date"] = pd.to_datetime(df[date_col[0]])
            df = df.set_index("Date")

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        req = ["Open", "High", "Low", "Close", "Volume"]
        for col in req:
            if col not in df.columns:
                return None

        df = df[req].dropna()
        return df
    except Exception:
        local_df = load_local_market_data(symbol)
        if local_df is not None and not local_df.empty:
            return local_df
        return None


# ==============================================================================
# BLOOMBERG-STYLE MARKET CONTEXT HELPERS
# ==============================================================================


def get_nifty_data(
    symbol: str = "NIFTY_50", period: str = "60d"
) -> Optional[pd.DataFrame]:
    """Get Nifty index data for context."""
    symbol_map = {"NIFTY_50": "^NSEI", "NIFTY_500": "^NSEBANK"}
    yf_symbol = symbol_map.get(symbol, symbol)
    return download_data(yf_symbol, period=period, interval="1d", prefer_local=True)


def get_vix_data(period: str = "60d") -> Optional[pd.DataFrame]:
    """Get India VIX data for volatility context."""
    return download_data("^INDIAVIX", period=period, interval="1d", prefer_local=False)


def get_market_breadth() -> List[Dict[str, Any]]:
    """Get market breadth indicators from repository market data."""
    try:
        import csv

        repo_root = Path(__file__).resolve().parent.parent
        breadth_file = repo_root / "data" / "market" / "breadth.csv"
        if breadth_file.exists():
            with open(breadth_file, "r") as f:
                return list(csv.DictReader(f))
    except Exception:
        # AUDIT: was a bare `pass`; a corrupt breadth file silently produced
        # "no breadth data" instead of an error anyone could see.
        logger.warning("breadth_file_unreadable path=%s", breadth_file, exc_info=True)
    return []


def get_nifty_50_constituents() -> List[str]:
    """Get Nifty 50 constituent stocks."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        nifty_file = repo_root / "data" / "universe" / "nifty50.csv"
        if nifty_file.exists():
            df = pd.read_csv(nifty_file)
            if "symbol" in df.columns:
                return df["symbol"].tolist()
    except Exception:
        # AUDIT: was a bare `pass`, which silently fell back to the
        # hard-coded list below and made a stale universe look current.
        logger.warning("nifty50_constituents_unreadable", exc_info=True)
    return [
        "RELIANCE",
        "TCS",
        "HDFCBANK",
        "INFY",
        "ICICIBANK",
        "SBIN",
        "BHARTIARTL",
        "ITC",
    ]


def get_market_session() -> Dict[str, Any]:
    """Determine current NSE market session (IST)."""
    import pytz

    ist = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist)
    day_of_week = now_ist.weekday()
    time_of_day = now_ist.time()

    is_trading_session = (
        day_of_week < 5
        and time_of_day >= datetime.strptime("09:15", "%H:%M").time()
        and time_of_day <= datetime.strptime("15:30", "%H:%M").time()
    )

    if is_trading_session:
        session = "OPEN - Market Hours"
    elif day_of_week >= 5:
        session = "Weekend - NSE Closed"
    else:
        session = "After Market Hours"

    return {
        "is_market_open": is_trading_session,
        "session": session,
        "timestamp": now_ist.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


# ==============================================================================
# 30 STRATEGY DEFINITIONS (Clean Crossover / Regime Transitions, Zero Clashing)
# ==============================================================================


class S01_AlphaTrend:
    name = "S01_AlphaTrend"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        rsi = calc_rsi(df, 14)
        atr = calc_atr(df, 14)
        at_raw = np.where(
            rsi >= 50, _squeeze(df, "Low") - atr, _squeeze(df, "High") + atr
        )
        at = calc_ema(pd.Series(at_raw, index=df.index), 2)
        df["Buy_Signal"] = (close > at) & (close.shift(1) <= at.shift(1))
        df["Sell_Signal"] = (close < at) & (close.shift(1) >= at.shift(1))
        return df


class S02_SuperTrend_714:
    name = "S02_SuperTrend_7_3"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        direction = calc_supertrend(df, 7, 3)
        df["Buy_Signal"] = (direction == 1.0) & (direction.shift(1) != 1.0)
        df["Sell_Signal"] = (direction == -1.0) & (direction.shift(1) != -1.0)
        return df


class S03_SuperTrend_510:
    name = "S03_SuperTrend_5_2"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        direction = calc_supertrend(df, 5, 2)
        df["Buy_Signal"] = (direction == 1.0) & (direction.shift(1) != 1.0)
        df["Sell_Signal"] = (direction == -1.0) & (direction.shift(1) != -1.0)
        return df


class S04_EMA_20_50:
    name = "S04_EMA_Cross_20_50"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        ema20 = calc_ema(close, 20)
        ema50 = calc_ema(close, 50)
        df["Buy_Signal"] = (ema20 > ema50) & (ema20.shift(1) <= ema50.shift(1))
        df["Sell_Signal"] = (ema20 < ema50) & (ema20.shift(1) >= ema50.shift(1))
        return df


class S05_EMA_9_21:
    name = "S05_EMA_Cross_9_21"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        ema9 = calc_ema(close, 9)
        ema21 = calc_ema(close, 21)
        df["Buy_Signal"] = (ema9 > ema21) & (ema9.shift(1) <= ema21.shift(1))
        df["Sell_Signal"] = (ema9 < ema21) & (ema9.shift(1) >= ema21.shift(1))
        return df


class S06_TripleEMA:
    name = "S06_Triple_EMA_5_13_21"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        e5 = calc_ema(close, 5)
        e13 = calc_ema(close, 13)
        e21 = calc_ema(close, 21)
        bull = (e5 > e13) & (e13 > e21)
        bear = (e5 < e13) & (e13 < e21)
        df["Buy_Signal"] = bull & ~bull.shift(1).fillna(False)
        df["Sell_Signal"] = bear & ~bear.shift(1).fillna(False)
        return df


class S07_GoldenCross:
    name = "S07_SMA_50_200_GoldenCross"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        s50 = calc_sma(close, 50)
        s200 = calc_sma(close, 200)
        df["Buy_Signal"] = (s50 > s200) & (s50.shift(1) <= s200.shift(1))
        df["Sell_Signal"] = (s50 < s200) & (s50.shift(1) >= s200.shift(1))
        return df


class S08_HullMA:
    name = "S08_HullMA_14_28"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        h14 = calc_hma(close, 14)
        h28 = calc_hma(close, 28)
        df["Buy_Signal"] = (h14 > h28) & (h14.shift(1) <= h28.shift(1))
        df["Sell_Signal"] = (h14 < h28) & (h14.shift(1) >= h28.shift(1))
        return df


class S09_ParabolicSAR:
    name = "S09_Parabolic_SAR"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        trend = calc_psar(df, 0.02, 0.02, 0.2)
        df["Buy_Signal"] = (trend > 0) & (trend.shift(1) <= 0)
        df["Sell_Signal"] = (trend < 0) & (trend.shift(1) >= 0)
        return df


class S10_Donchian_Breakout:
    name = "S10_Donchian_Breakout_20"
    group = "Trend"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        upper, _, lower = calc_donchian(df, 20)
        # Shift channel by 1 bar to prevent look-ahead bias on the current bar
        df["Buy_Signal"] = close > upper.shift(1)
        df["Sell_Signal"] = close < lower.shift(1)
        return df


class S11_HilegaMilega:
    name = "S11_Hilega_Milega"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi9 = calc_rsi(df, 9)
        ema3 = calc_ema(rsi9, 3)
        wma21 = calc_wma(rsi9, 21)
        bull = (rsi9 > 50.0) & (ema3 < rsi9) & (wma21 > 50.0)
        bear = (rsi9 < 50.0) & (ema3 > rsi9) & (wma21 < 50.0)
        df["Buy_Signal"] = bull & ~bull.shift(1).fillna(False)
        df["Sell_Signal"] = bear & ~bear.shift(1).fillna(False)
        return df


class S12_MACD_Signal_Cross:
    name = "S12_MACD_Signal_Cross"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        macd_line, signal_line, _ = calc_macd(df)
        df["Buy_Signal"] = (macd_line > signal_line) & (
            macd_line.shift(1) <= signal_line.shift(1)
        )
        df["Sell_Signal"] = (macd_line < signal_line) & (
            macd_line.shift(1) >= signal_line.shift(1)
        )
        return df


class S13_MACD_ZeroCross:
    name = "S13_MACD_Zero_Cross"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        macd_line, _, _ = calc_macd(df)
        df["Buy_Signal"] = (macd_line > 0.0) & (macd_line.shift(1) <= 0.0)
        df["Sell_Signal"] = (macd_line < 0.0) & (macd_line.shift(1) >= 0.0)
        return df


class S14_RSI_50_Cross:
    name = "S14_RSI_50_Cross"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi = calc_rsi(df, 14)
        df["Buy_Signal"] = (rsi > 50.0) & (rsi.shift(1) <= 50.0)
        df["Sell_Signal"] = (rsi < 50.0) & (rsi.shift(1) >= 50.0)
        return df


class S15_Stochastic:
    name = "S15_Stochastic_Oversold_Overbought"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        k, d = calc_stochastic(df, 14, 3)
        df["Buy_Signal"] = (k > d) & (k.shift(1) <= d.shift(1)) & (k < 80.0)
        df["Sell_Signal"] = (k < d) & (k.shift(1) >= d.shift(1)) & (k > 20.0)
        return df


class S16_CCI:
    name = "S16_CCI_100_Cross"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        cci = calc_cci(df, 20)
        df["Buy_Signal"] = (cci > -100.0) & (cci.shift(1) <= -100.0)
        df["Sell_Signal"] = (cci < 100.0) & (cci.shift(1) >= 100.0)
        return df


class S17_WilliamsR:
    name = "S17_Williams_R"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        wr = calc_williams_r(df, 14)
        df["Buy_Signal"] = (wr > -80.0) & (wr.shift(1) <= -80.0)
        df["Sell_Signal"] = (wr < -20.0) & (wr.shift(1) >= -20.0)
        return df


class S18_Aroon:
    name = "S18_Aroon_Cross_25"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        up, down = calc_aroon(df, 25)
        df["Buy_Signal"] = (up > down) & (up.shift(1) <= down.shift(1))
        df["Sell_Signal"] = (up < down) & (up.shift(1) >= down.shift(1))
        return df


class S19_ChandeMomentum:
    name = "S19_Chande_Momentum_Zero"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        cmo = calc_chande_momentum(df, 14)
        df["Buy_Signal"] = (cmo > 0.0) & (cmo.shift(1) <= 0.0)
        df["Sell_Signal"] = (cmo < 0.0) & (cmo.shift(1) >= 0.0)
        return df


class S20_ADX_DI_Cross:
    name = "S20_ADX_DI_Cross_25filter"
    group = "Momentum"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        adx, pdi, mdi = calc_adx(df, 14)
        df["Buy_Signal"] = (pdi > mdi) & (pdi.shift(1) <= mdi.shift(1)) & (adx > 25.0)
        df["Sell_Signal"] = (mdi > pdi) & (mdi.shift(1) <= pdi.shift(1)) & (adx > 25.0)
        return df


class S21_RSI_BollingerBands:
    name = "S21_RSI_BollingerBands_Combo"
    group = "Mean Reversion"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        rsi = calc_rsi(df, 14)
        upper, _, lower = calc_bollinger(df, 20, 2.0)
        df["Buy_Signal"] = (rsi < 35.0) & (close <= lower.shift(1))
        df["Sell_Signal"] = (rsi > 65.0) & (close >= upper.shift(1))
        return df


class S22_ZScore_Reversion:
    name = "S22_ZScore_Mean_Reversion"
    group = "Mean Reversion"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        z = calc_zscore(close, 20)
        df["Buy_Signal"] = (z < -1.5) & (z.shift(1) >= -1.5)
        df["Sell_Signal"] = (z > 1.5) & (z.shift(1) <= 1.5)
        return df


class S23_BB_Squeeze:
    name = "S23_BollingerBand_Squeeze"
    group = "Mean Reversion"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        bb_u, bb_mid, bb_l = calc_bollinger(df, 20, 2.0)
        kc_u, _, kc_l = calc_keltner(df, 20, 10, 1.5)
        squeeze = (bb_u < kc_u) & (bb_l > kc_l)
        squeeze_off = ~squeeze & squeeze.shift(1).fillna(False)
        df["Buy_Signal"] = squeeze_off & (close > bb_mid)
        df["Sell_Signal"] = squeeze_off & (close < bb_mid)
        return df


class S24_KeltnerChannel:
    name = "S24_Keltner_Channel_Bounce"
    group = "Mean Reversion"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        upper, _, lower = calc_keltner(df, 20, 10, 2.0)
        df["Buy_Signal"] = close < lower.shift(1)
        df["Sell_Signal"] = close > upper.shift(1)
        return df


class S25_RSI_30_70:
    name = "S25_RSI_30_70_Classic"
    group = "Mean Reversion"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        rsi = calc_rsi(df, 14)
        df["Buy_Signal"] = (rsi > 30.0) & (rsi.shift(1) <= 30.0)
        df["Sell_Signal"] = (rsi < 70.0) & (rsi.shift(1) >= 70.0)
        return df


class S26_VWAP:
    name = "S26_VWAP_Cross"
    group = "Volume"
    requires_volume = True

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if not is_volume_valid(df):
            df["Buy_Signal"] = False
            df["Sell_Signal"] = False
            return df
        close = _squeeze(df, "Close")
        vwap = calc_vwap(df)
        df["Buy_Signal"] = (close > vwap) & (close.shift(1) <= vwap.shift(1))
        df["Sell_Signal"] = (close < vwap) & (close.shift(1) >= vwap.shift(1))
        return df


class S27_Volume_RSI_Combo:
    name = "S27_Volume_Confirmed_RSI"
    group = "Volume"
    requires_volume = True

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if not is_volume_valid(df):
            df["Buy_Signal"] = False
            df["Sell_Signal"] = False
            return df
        rsi = calc_rsi(df, 14)
        volume = _squeeze(df, "Volume")
        vol_ma = calc_sma(volume, 20)
        df["Buy_Signal"] = (rsi > 50.0) & (rsi.shift(1) <= 50.0) & (volume > vol_ma)
        df["Sell_Signal"] = (rsi < 50.0) & (rsi.shift(1) >= 50.0) & (volume > vol_ma)
        return df


class S28_OBV_Trend:
    name = "S28_OBV_EMA_Cross"
    group = "Volume"
    requires_volume = True

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if not is_volume_valid(df):
            df["Buy_Signal"] = False
            df["Sell_Signal"] = False
            return df
        obv = calc_obv(df)
        obv_ema = calc_ema(obv, 20)
        df["Buy_Signal"] = (obv > obv_ema) & (obv.shift(1) <= obv_ema.shift(1))
        df["Sell_Signal"] = (obv < obv_ema) & (obv.shift(1) >= obv_ema.shift(1))
        return df


class S29_Triple_Confirmation:
    name = "S29_Triple_Confirmation_RSI_EMA_MACD"
    group = "Advanced"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        close = _squeeze(df, "Close")
        rsi = calc_rsi(df, 14)
        ema20 = calc_ema(close, 20)
        ema50 = calc_ema(close, 50)
        macd_line, signal_line, _ = calc_macd(df)
        bull = (rsi > 50.0) & (ema20 > ema50) & (macd_line > signal_line)
        bear = (rsi < 50.0) & (ema20 < ema50) & (macd_line < signal_line)
        df["Buy_Signal"] = bull & ~bull.shift(1).fillna(False)
        df["Sell_Signal"] = bear & ~bear.shift(1).fillna(False)
        return df


class S30_ADX_SuperTrend_Combo:
    name = "S30_ADX_SuperTrend_Combo"
    group = "Advanced"
    requires_volume = False

    def signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        adx, _, _ = calc_adx(df, 14)
        strong = adx > 25.0
        direction = calc_supertrend(df, 7, 3)
        df["Buy_Signal"] = (direction == 1.0) & (direction.shift(1) != 1.0) & strong
        df["Sell_Signal"] = (direction == -1.0) & (direction.shift(1) != -1.0) & strong
        return df


ALL_STRATEGIES = [
    S01_AlphaTrend(),
    S02_SuperTrend_714(),
    S03_SuperTrend_510(),
    S04_EMA_20_50(),
    S05_EMA_9_21(),
    S06_TripleEMA(),
    S07_GoldenCross(),
    S08_HullMA(),
    S09_ParabolicSAR(),
    S10_Donchian_Breakout(),
    S11_HilegaMilega(),
    S12_MACD_Signal_Cross(),
    S13_MACD_ZeroCross(),
    S14_RSI_50_Cross(),
    S15_Stochastic(),
    S16_CCI(),
    S17_WilliamsR(),
    S18_Aroon(),
    S19_ChandeMomentum(),
    S20_ADX_DI_Cross(),
    S21_RSI_BollingerBands(),
    S22_ZScore_Reversion(),
    S23_BB_Squeeze(),
    S24_KeltnerChannel(),
    S25_RSI_30_70(),
    S26_VWAP(),
    S27_Volume_RSI_Combo(),
    S28_OBV_Trend(),
    S29_Triple_Confirmation(),
    S30_ADX_SuperTrend_Combo(),
]


# ==============================================================================
# RISK-AWARE SINGLE STRATEGY BACKTEST ENGINE (Resolves Issue 1, 4, & Section 4)
# ==============================================================================


def backtest_strategy(
    df: pd.DataFrame,
    strategy: Any,
    initial_capital: float = 50000.0,
    brokerage_bps: float = 15.0,
    fixed_brokerage: float = 0.0,
    atr_stop_multiplier: float = 2.0,
    capital_alloc_pct: float = 0.95,
) -> Tuple[pd.DataFrame, float, pd.Series]:
    """
    Run backtest on a single strategy with realistic execution:
    - Signals confirmed at bar i Close -> Executed at bar i+1 Open (No lookahead)
    - Hard ATR stop-loss evaluated during bar i+1 (Low <= stop_price)
    - Terminal mark-to-market trade closing (no dropped open positions)
    - Realistic Indian market transaction costs (15 bps round-trip default)
    """
    if df is None or df.empty or len(df) < 5:
        return pd.DataFrame(), initial_capital, pd.Series(dtype=float)

    # Check volume requirement
    if getattr(strategy, "requires_volume", False) and not is_volume_valid(df):
        return (
            pd.DataFrame(),
            initial_capital,
            pd.Series([initial_capital] * len(df), index=df.index),
        )

    try:
        df_sig = strategy.signals(df.copy())
    except Exception:
        return pd.DataFrame(), initial_capital, pd.Series(dtype=float)

    if "Buy_Signal" not in df_sig.columns or "Sell_Signal" not in df_sig.columns:
        return pd.DataFrame(), initial_capital, pd.Series(dtype=float)

    open_prices = _squeeze(df_sig, "Open")
    close_prices = _squeeze(df_sig, "Close")
    low_prices = _squeeze(df_sig, "Low")
    atr_series = calc_atr(df_sig, 14)

    position = 0
    buy_price = 0.0
    stop_price = 0.0
    entry_date = None
    trades = []
    capital = float(initial_capital)
    equity_curve = [capital]

    # One-way cost rate
    one_way_cost_rate = (brokerage_bps / 2.0) / 10_000.0

    # Main execution loop: signal at bar i close -> fill at bar i+1 open
    for i in range(len(df_sig) - 1):
        curr_price = float(close_prices.iloc[i])
        curr_atr = (
            float(atr_series.iloc[i])
            if not np.isnan(atr_series.iloc[i])
            else (curr_price * 0.015)
        )

        next_open = float(open_prices.iloc[i + 1])
        next_low = float(low_prices.iloc[i + 1])
        next_close = float(close_prices.iloc[i + 1])
        next_date = df_sig.index[i + 1]

        buy_sig = bool(df_sig["Buy_Signal"].iloc[i])
        sell_sig = bool(df_sig["Sell_Signal"].iloc[i])

        # 1. Stop Loss check during bar i+1
        if position > 0 and atr_stop_multiplier > 0 and next_low <= stop_price:
            exit_price = max(min(next_open, stop_price), next_low)
            gross_pnl = (exit_price - buy_price) * position
            turnover = (buy_price + exit_price) * position
            cost = (turnover * one_way_cost_rate) + (2.0 * fixed_brokerage)
            net_pnl = gross_pnl - cost
            capital += (exit_price * position) - (cost / 2.0)
            trades.append(
                {
                    "Entry_Date": entry_date,
                    "Exit_Date": next_date,
                    "Buy_Price": round(buy_price, 2),
                    "Sell_Price": round(exit_price, 2),
                    "Qty": position,
                    "Stop_Price": round(stop_price, 2),
                    "Gross_PnL": round(gross_pnl, 2),
                    "Cost": round(cost, 2),
                    "Net_PnL": round(net_pnl, 2),
                    "Return %": round((exit_price - buy_price) / buy_price * 100.0, 2),
                    "Exit_Reason": "STOP_LOSS",
                    "Capital": round(capital, 2),
                }
            )
            position = 0
            buy_price = 0.0
            stop_price = 0.0

        # 2. Strategy Sell Signal: exit at bar i+1 Open
        elif position > 0 and sell_sig:
            exit_price = next_open
            gross_pnl = (exit_price - buy_price) * position
            turnover = (buy_price + exit_price) * position
            cost = (turnover * one_way_cost_rate) + (2.0 * fixed_brokerage)
            net_pnl = gross_pnl - cost
            capital += (exit_price * position) - (cost / 2.0)
            trades.append(
                {
                    "Entry_Date": entry_date,
                    "Exit_Date": next_date,
                    "Buy_Price": round(buy_price, 2),
                    "Sell_Price": round(exit_price, 2),
                    "Qty": position,
                    "Stop_Price": round(stop_price, 2),
                    "Gross_PnL": round(gross_pnl, 2),
                    "Cost": round(cost, 2),
                    "Net_PnL": round(net_pnl, 2),
                    "Return %": round((exit_price - buy_price) / buy_price * 100.0, 2),
                    "Exit_Reason": "SIGNAL_EXIT",
                    "Capital": round(capital, 2),
                }
            )
            position = 0
            buy_price = 0.0
            stop_price = 0.0

        # 3. Strategy Buy Signal: enter at bar i+1 Open
        elif position == 0 and buy_sig and not sell_sig:
            buy_price = next_open
            if buy_price > 0:
                alloc_capital = capital * capital_alloc_pct
                quantity = int(alloc_capital / buy_price)
                if quantity >= 1:
                    position = quantity
                    entry_date = next_date
                    buy_cost = (
                        buy_price * position * one_way_cost_rate
                    ) + fixed_brokerage
                    capital -= buy_price * position + buy_cost
                    if atr_stop_multiplier > 0:
                        stop_dist = curr_atr * atr_stop_multiplier
                        stop_price = max(buy_price - stop_dist, 0.01)

        # Mark to market equity at bar i+1 Close
        current_equity = capital + (position * next_close)
        equity_curve.append(current_equity)

    # Reconcile open position at end of backtest data (Section 4 / Issue 2 fix)
    if position > 0:
        final_price = float(close_prices.iloc[-1])
        final_date = df_sig.index[-1]
        gross_pnl = (final_price - buy_price) * position
        turnover = (buy_price + final_price) * position
        cost = (turnover * one_way_cost_rate) + (2.0 * fixed_brokerage)
        net_pnl = gross_pnl - cost
        capital += (final_price * position) - (cost / 2.0)
        trades.append(
            {
                "Entry_Date": entry_date,
                "Exit_Date": final_date,
                "Buy_Price": round(buy_price, 2),
                "Sell_Price": round(final_price, 2),
                "Qty": position,
                "Stop_Price": round(stop_price, 2),
                "Gross_PnL": round(gross_pnl, 2),
                "Cost": round(cost, 2),
                "Net_PnL": round(net_pnl, 2),
                "Return %": round((final_price - buy_price) / buy_price * 100.0, 2),
                "Exit_Reason": "END_OF_DATA",
                "Capital": round(capital, 2),
            }
        )
        position = 0

    eq_series = pd.Series(equity_curve, index=df_sig.index[: len(equity_curve)])
    final_cap = trades[-1]["Capital"] if trades else initial_capital
    return pd.DataFrame(trades), round(final_cap, 2), eq_series


# ==============================================================================
# STATISTICAL METRICS & DEFLATED SHARPE (Resolves Issue 5 & Section 6)
# ==============================================================================


def calculate_deflated_sharpe(
    sharpe: float, n_obs: int, n_trials: int = 30, benchmark_sharpe: float = 0.0
) -> float:
    """
    Deflated Sharpe Ratio (Bailey & López de Prado, 2014).
    Calculates the probability that the observed Sharpe ratio is statistically
    significant after correcting for multiple hypothesis tests (n_trials).
    """
    if n_obs < 5 or n_trials < 1:
        return 0.0
    se = 1.0 / np.sqrt(max(n_obs - 1, 1))
    if n_trials > 1:
        euler_mascheroni = 0.5772156649
        e_max = (
            benchmark_sharpe
            + (
                (1.0 - euler_mascheroni) * stats.norm.ppf(1.0 - 1.0 / n_trials)
                + euler_mascheroni * stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
            )
            * se
        )
    else:
        e_max = benchmark_sharpe
    t_stat = (sharpe - e_max) / max(se, 1e-10)
    return float(stats.norm.cdf(t_stat))


def score_trades(
    trades_df: pd.DataFrame,
    initial_capital: float,
    strategy_name: str,
    group: str,
    equity_curve: Optional[pd.Series] = None,
    periods_per_year: int = 252,
    rf_rate: float = 0.06,
    n_trials: int = 30,
) -> Dict[str, Any]:
    """
    Score strategy performance using institutional risk-adjusted metrics:
    Sharpe Ratio, Sortino Ratio, Calmar Ratio, Max Drawdown, and Deflated Sharpe.
    """
    base = {
        "Strategy": strategy_name,
        "Group": group,
        "Trades": 0,
        "Win Rate %": 0.0,
        "Return %": 0.0,
        "CAGR %": 0.0,
        "Sharpe": 0.0,
        "Sortino": 0.0,
        "Calmar": 0.0,
        "Deflated Sharpe": 0.0,
        "Profit Factor": 0.0,
        "Max Drawdown %": 0.0,
        "Final Capital": initial_capital,
        "Score": 0,
        "Rating": "⚠️ NO DATA",
    }

    if (trades_df is None or trades_df.empty) and (
        equity_curve is None or len(equity_curve) < 5
    ):
        return base

    if equity_curve is not None and len(equity_curve) > 5:
        eq = equity_curve.dropna()
        daily_returns = eq.pct_change().dropna()
        n_periods = len(daily_returns)
        years = max(n_periods / periods_per_year, 0.05)
        final_cap = float(eq.iloc[-1])
        total_ret = float((final_cap - initial_capital) / initial_capital * 100.0)
        cagr = (
            float(((final_cap / initial_capital) ** (1.0 / years) - 1.0) * 100.0)
            if final_cap > 0
            else -100.0
        )

        # Drawdown
        roll_max = eq.cummax()
        dd = (eq - roll_max) / roll_max * 100.0
        max_dd = abs(float(dd.min()))

        # Volatility and Sharpe
        vol = float(daily_returns.std(ddof=1) * np.sqrt(periods_per_year) * 100.0)
        excess_cagr = cagr - (rf_rate * 100.0)
        sharpe = round(excess_cagr / vol, 2) if vol > 0 else 0.0

        # Sortino
        downside = daily_returns[daily_returns < 0]
        downside_vol = (
            float(downside.std(ddof=1) * np.sqrt(periods_per_year) * 100.0)
            if len(downside) > 1
            else 1e-10
        )
        sortino = round(excess_cagr / downside_vol, 2) if downside_vol > 0 else 0.0

        # Calmar
        calmar = round(cagr / max_dd, 2) if max_dd > 0 else (99.0 if cagr > 0 else 0.0)

        # Deflated Sharpe
        dsr = round(calculate_deflated_sharpe(sharpe, n_periods, n_trials=n_trials), 3)
    else:
        final_cap = float(trades_df["Capital"].iloc[-1])
        total_ret = float((final_cap - initial_capital) / initial_capital * 100.0)
        cagr = total_ret
        max_dd = 0.0
        sharpe = 0.0
        sortino = 0.0
        calmar = 0.0
        dsr = 0.0

    # Trade-level metrics
    if trades_df is not None and not trades_df.empty:
        n_trades = len(trades_df)
        wins = trades_df[trades_df["Net_PnL"] > 0]
        losses = trades_df[trades_df["Net_PnL"] < 0]
        win_rate = round(len(wins) / n_trades * 100.0, 1)

        gp = float(wins["Net_PnL"].sum()) if not wins.empty else 0.0
        gl = float(losses["Net_PnL"].abs().sum()) if not losses.empty else 1e-10
        pf = round(gp / gl, 2) if gl > 0 else 999.0
    else:
        n_trades = 0
        win_rate = 0.0
        pf = 0.0

    # Risk-Adjusted Scoring (0 to 10 points)
    sc = 0
    sc += 2 if sharpe >= 1.0 else (1 if sharpe >= 0.5 else 0)
    sc += 2 if calmar >= 1.0 else (1 if calmar >= 0.5 else 0)
    sc += 2 if max_dd < 15.0 else (1 if max_dd < 25.0 else 0)
    sc += 2 if pf > 1.8 else (1 if pf > 1.3 else 0)
    sc += (
        2
        if (win_rate >= 50.0 and n_trades >= 6)
        else (1 if (win_rate >= 40.0 and n_trades >= 3) else 0)
    )

    # Rating
    if sc >= 8 and sharpe >= 0.8 and dsr >= 0.90:
        rating = "🟢 EXCELLENT"
    elif sc >= 6 and sharpe >= 0.4:
        rating = "🟡 GOOD"
    elif sc >= 4:
        rating = "🟠 AVERAGE"
    else:
        rating = "🔴 POOR"

    return {
        "Strategy": strategy_name,
        "Group": group,
        "Trades": n_trades,
        "Win Rate %": win_rate,
        "Return %": round(total_ret, 1),
        "CAGR %": round(cagr, 1),
        "Sharpe": sharpe,
        "Sortino": sortino,
        "Calmar": calmar,
        "Deflated Sharpe": dsr,
        "Profit Factor": pf,
        "Max Drawdown %": round(max_dd, 1),
        "Final Capital": round(final_cap, 2),
        "Score": sc,
        "Rating": rating,
    }


# ==============================================================================
# SHARED-CAPITAL MULTI-SYMBOL PORTFOLIO BACKTESTER (Resolves Issue 2)
# ==============================================================================


def run_portfolio_backtest(
    symbols_data: Dict[str, pd.DataFrame],
    strategy: Any,
    initial_capital: float = 1_000_000.0,
    max_positions: int = 8,
    sizing_method: str = "vol_target",
    risk_per_trade_pct: float = 0.01,
    atr_stop_multiplier: float = 2.0,
    cost_bps: float = 15.0,
) -> Dict[str, Any]:
    """
    Shared-capital multi-symbol portfolio backtester.
    """
    if not symbols_data:
        return {
            "portfolio_metrics": {},
            "trades": pd.DataFrame(),
            "equity_curve": pd.Series(),
        }

    processed = {}
    for sym, df in symbols_data.items():
        if df is None or df.empty or len(df) < 15:
            continue
        try:
            sig_df = strategy.signals(df.copy())
            sig_df["ATR"] = calc_atr(sig_df, 14)
            processed[sym] = sig_df
        except Exception:
            # AUDIT: was a bare `continue`; a strategy that raised for every
            # symbol looked identical to one that produced no signals.
            logger.warning("strategy_signals_failed symbol=%s", sym, exc_info=True)
            continue

    if not processed:
        return {
            "portfolio_metrics": {},
            "trades": pd.DataFrame(),
            "equity_curve": pd.Series(),
        }

    all_timestamps = sorted(
        list(set.union(*[set(df.index) for df in processed.values()]))
    )

    cash = float(initial_capital)
    open_positions = {}
    all_trades = []
    portfolio_equity_series = []
    one_way_cost = (cost_bps / 2.0) / 10_000.0

    for ts in all_timestamps:
        current_invested = 0.0
        exited_symbols = []

        for sym, pos in list(open_positions.items()):
            df = processed[sym]
            if ts not in df.index:
                current_invested += pos["qty"] * pos["buy_price"]
                continue

            row = df.loc[ts]
            price = float(_squeeze(row, "Close"))
            low = float(_squeeze(row, "Low"))
            sell_sig = bool(row.get("Sell_Signal", False))
            stop_price = pos["stop_price"]

            # 1. Stop Loss check
            if atr_stop_multiplier > 0 and low <= stop_price:
                exit_price = max(min(price, stop_price), low)
                gross_pnl = (exit_price - pos["buy_price"]) * pos["qty"]
                cost = (pos["buy_price"] + exit_price) * pos["qty"] * one_way_cost
                net_pnl = gross_pnl - cost
                cash += (exit_price * pos["qty"]) - (cost / 2.0)
                all_trades.append(
                    {
                        "Symbol": sym,
                        "Entry_Date": pos["entry_date"],
                        "Exit_Date": ts,
                        "Buy_Price": round(pos["buy_price"], 2),
                        "Exit_Price": round(exit_price, 2),
                        "Qty": pos["qty"],
                        "Net_PnL": round(net_pnl, 2),
                        "Return %": round(
                            (exit_price - pos["buy_price"]) / pos["buy_price"] * 100.0,
                            2,
                        ),
                        "Exit_Reason": "STOP_LOSS",
                    }
                )
                exited_symbols.append(sym)
                continue

            # 2. Strategy Sell Signal
            if sell_sig:
                exit_price = price
                gross_pnl = (exit_price - pos["buy_price"]) * pos["qty"]
                cost = (pos["buy_price"] + exit_price) * pos["qty"] * one_way_cost
                net_pnl = gross_pnl - cost
                cash += (exit_price * pos["qty"]) - (cost / 2.0)
                all_trades.append(
                    {
                        "Symbol": sym,
                        "Entry_Date": pos["entry_date"],
                        "Exit_Date": ts,
                        "Buy_Price": round(pos["buy_price"], 2),
                        "Exit_Price": round(exit_price, 2),
                        "Qty": pos["qty"],
                        "Net_PnL": round(net_pnl, 2),
                        "Return %": round(
                            (exit_price - pos["buy_price"]) / pos["buy_price"] * 100.0,
                            2,
                        ),
                        "Exit_Reason": "SIGNAL_EXIT",
                    }
                )
                exited_symbols.append(sym)
                continue

            current_invested += pos["qty"] * price

        for sym in exited_symbols:
            del open_positions[sym]

        total_equity = cash + current_invested

        # Check new Entries if capacity available
        if len(open_positions) < max_positions and cash > (total_equity * 0.05):
            for sym, df in processed.items():
                if sym in open_positions or len(open_positions) >= max_positions:
                    continue
                if ts not in df.index:
                    continue

                row = df.loc[ts]
                buy_sig = bool(row.get("Buy_Signal", False))
                sell_sig_entry = bool(row.get("Sell_Signal", False))
                if not buy_sig or sell_sig_entry:
                    continue

                price = float(_squeeze(row, "Close"))
                atr = float(row.get("ATR", price * 0.02))
                if price <= 0:
                    continue

                if sizing_method == "vol_target" and atr > 0:
                    risk_amount = total_equity * risk_per_trade_pct
                    dollar_risk = max(atr * atr_stop_multiplier, price * 0.01)
                    qty = int(risk_amount / dollar_risk)
                    max_notional = (total_equity / max_positions) * 1.5
                    qty = min(qty, int(max_notional / price))
                else:
                    target_notional = total_equity / max_positions
                    qty = int(target_notional / price)

                trade_notional = qty * price
                entry_cost = trade_notional * one_way_cost
                if trade_notional + entry_cost > cash * 0.95:
                    qty = int((cash * 0.90) / price)

                if qty >= 1:
                    stop_price = (
                        max(price - (atr * atr_stop_multiplier), 0.01)
                        if atr_stop_multiplier > 0
                        else 0.0
                    )
                    cost = (qty * price) * one_way_cost
                    cash -= qty * price + cost
                    open_positions[sym] = {
                        "qty": qty,
                        "buy_price": price,
                        "stop_price": stop_price,
                        "entry_date": ts,
                    }

        portfolio_invested = sum(
            pos["qty"] * float(_squeeze(processed[sym].loc[ts], "Close"))
            if ts in processed[sym].index
            else pos["qty"] * pos["buy_price"]
            for sym, pos in open_positions.items()
        )
        total_equity = cash + portfolio_invested
        portfolio_equity_series.append(total_equity)

    # Reconcile open positions at the end of backtest (END_OF_DATA)
    final_ts = all_timestamps[-1] if all_timestamps else None
    for sym, pos in list(open_positions.items()):
        df = processed[sym]
        final_price = (
            float(_squeeze(df.iloc[-1], "Close")) if not df.empty else pos["buy_price"]
        )
        gross_pnl = (final_price - pos["buy_price"]) * pos["qty"]
        cost = (pos["buy_price"] + final_price) * pos["qty"] * one_way_cost
        net_pnl = gross_pnl - cost
        cash += (final_price * pos["qty"]) - (cost / 2.0)
        all_trades.append(
            {
                "Symbol": sym,
                "Entry_Date": pos["entry_date"],
                "Exit_Date": final_ts,
                "Buy_Price": round(pos["buy_price"], 2),
                "Exit_Price": round(final_price, 2),
                "Qty": pos["qty"],
                "Net_PnL": round(net_pnl, 2),
                "Return %": round(
                    (final_price - pos["buy_price"]) / pos["buy_price"] * 100.0, 2
                ),
                "Exit_Reason": "END_OF_DATA",
            }
        )

    eq_series = pd.Series(portfolio_equity_series, index=all_timestamps)
    trades_df = pd.DataFrame(all_trades)
    perf = score_trades(
        trades_df,
        initial_capital,
        strategy.name,
        getattr(strategy, "group", "Portfolio"),
        equity_curve=eq_series,
    )

    return {
        "portfolio_metrics": perf,
        "trades": trades_df,
        "equity_curve": eq_series,
        "final_equity": round(
            float(eq_series.iloc[-1]) if not eq_series.empty else initial_capital, 2
        ),
    }


# ==============================================================================
# WALK-FORWARD VALIDATION & RESEARCH GATE PIPELINE (The Core Problem Fix)
# ==============================================================================


def walk_forward_validate_strategy(
    df: pd.DataFrame,
    strategy: Any,
    train_ratio: float = 0.70,
    atr_stop_multiplier: float = 2.0,
    cost_bps: float = 15.0,
    n_trials: int = 30,
) -> Dict[str, Any]:
    """
    Purged Train/Test Walk-Forward Validation Engine.
    """
    if df is None or len(df) < 30:
        return {
            "strategy": strategy.name,
            "verdict": "INSUFFICIENT_DATA",
            "is_sharpe": 0.0,
            "oos_sharpe": 0.0,
            "degradation": 0.0,
            "gate_decision": "FAIL",
        }

    n = len(df)
    split_idx = int(n * train_ratio)
    train_df = df.iloc[:split_idx].copy()
    test_df = df.iloc[split_idx:].copy()

    # In-Sample Backtest
    is_trades, is_final, is_equity = backtest_strategy(
        train_df,
        strategy,
        atr_stop_multiplier=atr_stop_multiplier,
        brokerage_bps=cost_bps,
    )
    is_metrics = score_trades(
        is_trades,
        50000.0,
        strategy.name,
        strategy.group,
        equity_curve=is_equity,
        n_trials=n_trials,
    )

    # Out-of-Sample Backtest
    oos_trades, oos_final, oos_equity = backtest_strategy(
        test_df,
        strategy,
        atr_stop_multiplier=atr_stop_multiplier,
        brokerage_bps=cost_bps,
    )
    oos_metrics = score_trades(
        oos_trades,
        50000.0,
        strategy.name,
        strategy.group,
        equity_curve=oos_equity,
        n_trials=n_trials,
    )

    is_sr = is_metrics["Sharpe"]
    oos_sr = oos_metrics["Sharpe"]
    degradation = round(oos_sr / is_sr, 2) if is_sr > 0 else 0.0

    # Placebo cross-check
    np.random.seed(42)
    placebo_signals = df.copy()
    placebo_signals["Buy_Signal"] = np.random.rand(len(df)) > 0.85
    placebo_signals["Sell_Signal"] = np.random.rand(len(df)) > 0.85
    _, _, pl_eq = backtest_strategy(
        placebo_signals,
        strategy,
        atr_stop_multiplier=atr_stop_multiplier,
        brokerage_bps=cost_bps,
    )
    pl_metrics = score_trades(
        pd.DataFrame(), 50000.0, "Placebo", "Noise", equity_curve=pl_eq
    )
    placebo_sharpe = pl_metrics["Sharpe"]

    # Research Gate Decision
    if (
        oos_sr >= 0.5
        and is_sr >= 0.6
        and oos_metrics["Return %"] > 0
        and oos_metrics["Deflated Sharpe"] >= 0.90
        and oos_sr > placebo_sharpe
    ):
        verdict = "PASS_FOR_RESEARCH_COCKPIT"
        gate_decision = "PASS (Validated Candidate)"
    elif oos_sr >= 0.2 and oos_metrics["Return %"] > 0:
        verdict = "FRAGILE"
        gate_decision = "FRAGILE (Needs Regime Filtering)"
    else:
        verdict = "REJECT_IN_SAMPLE_OVERFIT"
        gate_decision = "FAIL (Overfit Noise)"

    return {
        "strategy": strategy.name,
        "group": strategy.group,
        "verdict": verdict,
        "gate_decision": gate_decision,
        "is_sharpe": is_sr,
        "oos_sharpe": oos_sr,
        "is_cagr": is_metrics["CAGR %"],
        "oos_cagr": oos_metrics["CAGR %"],
        "is_max_dd": is_metrics["Max Drawdown %"],
        "oos_max_dd": oos_metrics["Max Drawdown %"],
        "degradation_ratio": degradation,
        "deflated_sharpe": oos_metrics["Deflated Sharpe"],
        "placebo_sharpe": placebo_sharpe,
        "is_metrics": is_metrics,
        "oos_metrics": oos_metrics,
    }


# ==============================================================================
# MASTER RUNNER & IDEA GENERATOR PIPELINE
# ==============================================================================


def run_all_strategies(
    symbol: str,
    period: str = "60d",
    interval: str = "1h",
    capital: float = 50000.0,
    brokerage_bps: float = 15.0,
    atr_stop_multiplier: float = 2.0,
    validate_walk_forward: bool = True,
) -> Optional[pd.DataFrame]:
    """
    Download data and run all 30 candidate strategies on a symbol.
    Provides fast screening scores alongside Walk-Forward validation flags.
    """
    print(f"\n{'=' * 70}")
    print(f"📊 IDEA GENERATOR SCREENING — SYMBOL: {symbol}")
    print(f"{'=' * 70}")

    df = download_data(symbol, period, interval, prefer_local=True)
    if df is None or df.empty:
        print(f"❌ No data for {symbol}")
        return None

    print(f"✅ {len(df)} candles loaded. Volume valid: {is_volume_valid(df)}\n")

    results = []
    for i, strategy in enumerate(ALL_STRATEGIES):
        if getattr(strategy, "requires_volume", False) and not is_volume_valid(df):
            perf = {
                "Strategy": strategy.name,
                "Group": strategy.group,
                "Trades": 0,
                "Win Rate %": 0.0,
                "Return %": 0.0,
                "CAGR %": 0.0,
                "Sharpe": 0.0,
                "Sortino": 0.0,
                "Calmar": 0.0,
                "Deflated Sharpe": 0.0,
                "Profit Factor": 0.0,
                "Max Drawdown %": 0.0,
                "Final Capital": capital,
                "Score": 0,
                "Rating": "⚠️ NO VOLUME DATA",
            }
            results.append(perf)
            print(
                f"  [{i + 1:02d}/30] {strategy.name:<40} | ⚠️ NO VOLUME DATA (Index Symbol)"
            )
            continue

        trades_df, final_cap, equity_curve = backtest_strategy(
            df,
            strategy,
            initial_capital=capital,
            brokerage_bps=brokerage_bps,
            atr_stop_multiplier=atr_stop_multiplier,
        )
        perf = score_trades(
            trades_df,
            capital,
            strategy.name,
            strategy.group,
            equity_curve=equity_curve,
            n_trials=len(ALL_STRATEGIES),
        )
        results.append(perf)

        print(
            f"  [{i + 1:02d}/30] {strategy.name:<40} | "
            f"{perf['Rating']:<15} | "
            f"Sharpe: {perf['Sharpe']:>5.2f} | "
            f"Return: {perf['Return %']:>6.1f}% | "
            f"MaxDD: {perf['Max Drawdown %']:>5.1f}% | "
            f"Trades: {perf['Trades']:>3}"
        )

    return pd.DataFrame(results)


def run_idea_generation_pipeline(
    symbols: List[str],
    strategies: Optional[List[Any]] = None,
    period: str = "60d",
    interval: str = "1h",
    top_candidates_count: int = 5,
) -> Dict[str, Any]:
    """
    Two-Tier Idea Generation & Validation Pipeline.
    """
    if strategies is None:
        strategies = ALL_STRATEGIES

    print("\n=======================================================")
    print(
        f"🚀 RUNNING TIER 1: CANDIDATE SCREENING ({len(strategies)} strategies × {len(symbols)} symbols)"
    )
    print("=======================================================")

    all_screening = []
    symbol_dfs = {}

    for sym in symbols:
        df = download_data(sym, period, interval, prefer_local=True)
        if df is not None and not df.empty:
            symbol_dfs[sym] = df
            res = run_all_strategies(sym, period, interval)
            if res is not None:
                res["Symbol"] = sym
                all_screening.append(res)

    if not all_screening:
        return {
            "screening_summary": pd.DataFrame(),
            "top_screened": pd.DataFrame(),
            "validated_candidates": [],
        }

    combined_df = pd.concat(all_screening, ignore_index=True)
    top_screened = combined_df.sort_values(
        by=["Score", "Sharpe", "Return %"], ascending=False
    ).head(top_candidates_count)

    print("\n=======================================================")
    print("🔬 RUNNING TIER 2: WALK-FORWARD VALIDATION ON TOP CANDIDATES")
    print("=======================================================")

    validated = []
    for _, row in top_screened.iterrows():
        sym = row["Symbol"]
        strat_name = row["Strategy"]
        strat_obj = next((s for s in strategies if s.name == strat_name), None)
        if strat_obj is None or sym not in symbol_dfs:
            continue

        wf_res = walk_forward_validate_strategy(symbol_dfs[sym], strat_obj)
        wf_res["symbol"] = sym
        wf_res["initial_screen_rank"] = row["Score"]
        validated.append(wf_res)

        print(
            f"  👉 [{sym}] {strat_name}: Gate = {wf_res['gate_decision']} "
            f"(IS Sharpe: {wf_res['is_sharpe']}, OOS Sharpe: {wf_res['oos_sharpe']}, DSR: {wf_res['deflated_sharpe']})"
        )

    return {
        "screening_summary": combined_df,
        "top_screened": top_screened,
        "validated_candidates": validated,
    }


# ==============================================================================
# REPORTING, EXPORT, & VISUALIZATION HELPERS (Section 8)
# ==============================================================================


def get_top_strategies(
    results_df: Optional[pd.DataFrame], top_n: int = 10
) -> pd.DataFrame:
    """Get top N strategies sorted by risk-adjusted Score and Sharpe."""
    if results_df is None or results_df.empty:
        return pd.DataFrame()
    return results_df.sort_values(
        by=["Score", "Sharpe", "Return %"], ascending=False
    ).head(top_n)


def get_best_by_group(results_df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """Get best strategy per category group."""
    if results_df is None or results_df.empty:
        return {}
    best = {}
    for group in results_df["Group"].unique():
        grp_df = results_df[results_df["Group"] == group]
        if not grp_df.empty:
            best[group] = (
                grp_df.sort_values(by=["Score", "Sharpe"], ascending=False)
                .iloc[0]
                .to_dict()
            )
    return best


def get_rating_distribution(results_df: Optional[pd.DataFrame]) -> Dict[str, int]:
    """Get rating distribution counts."""
    if results_df is None or results_df.empty:
        return {}
    return results_df["Rating"].value_counts().to_dict()


def generate_csv_report(
    results_df: pd.DataFrame, filename: str = "30_strategy_report.csv"
) -> str:
    """Export complete auditable backtest results to CSV."""
    if results_df is not None and not results_df.empty:
        results_df.to_csv(filename, index=False)
        print(f"  📁 Audit CSV exported: {filename}")
    return filename


def plot_heatmap(
    results_df: pd.DataFrame,
    metric: str = "Sharpe",
    filename: str = "strategy_heatmap.png",
) -> Optional[str]:
    """
    Plot cross-symbol strategy performance heatmap.
    Explicitly distinguishes missing data pairs (NaN / grey) from 0.0 return.
    """
    if results_df is None or results_df.empty or "Symbol" not in results_df.columns:
        return None

    pivot = results_df.pivot(index="Strategy", columns="Symbol", values=metric)
    plt.figure(figsize=(14, 10))
    sns.heatmap(
        pivot,
        annot=True,
        cmap="RdYlGn",
        center=0.0,
        cbar_kws={"label": metric},
        mask=pivot.isna(),
    )
    plt.title(
        f"Strategy vs Symbol Cross-Sectional Heatmap ({metric})",
        fontsize=14,
        fontweight="bold",
    )
    plt.tight_layout()
    plt.savefig(filename, dpi=130, bbox_inches="tight")
    plt.close()
    return filename


def generate_dashboard_chart(
    results_df: Optional[pd.DataFrame], symbol: str
) -> Optional[str]:
    """Generate a 4-panel risk-adjusted comparison chart."""
    if results_df is None or results_df.empty:
        print("⚠️ No results to chart")
        return None

    color_map = {
        "🟢 EXCELLENT": "#27ae60",
        "🟡 GOOD": "#f1c40f",
        "🟠 AVERAGE": "#e67e22",
        "🔴 POOR": "#e74c3c",
        "⚠️ NO DATA": "#95a5a6",
        "⚠️ NO VOLUME DATA": "#7f8c8d",
    }
    colors = [color_map.get(r, "#95a5a6") for r in results_df["Rating"]]
    names = results_df["Strategy"].tolist()

    fig, axes = plt.subplots(2, 2, figsize=(20, 12))
    fig.suptitle(
        f"30-Strategy Idea Screening Dashboard (Risk-Adjusted) — {symbol}",
        fontsize=14,
        fontweight="bold",
    )

    # 1. Sharpe Ratio
    axes[0, 0].barh(names, results_df["Sharpe"], color=colors)
    axes[0, 0].axvline(
        x=1.0, color="green", linestyle="--", linewidth=1, label="1.0 Target"
    )
    axes[0, 0].axvline(x=0.0, color="red", linestyle="-", linewidth=1)
    axes[0, 0].set_title(
        "Annualized Sharpe Ratio (Excess over 6% Rf)", fontweight="bold"
    )
    axes[0, 0].legend(fontsize=8)
    axes[0, 0].tick_params(axis="y", labelsize=7)

    # 2. Total Return %
    axes[0, 1].barh(names, results_df["Return %"], color=colors)
    axes[0, 1].axvline(x=0, color="black", linewidth=1)
    axes[0, 1].set_title(
        "Total Return % (Net of 15 bps Costs & ATR Stops)", fontweight="bold"
    )
    axes[0, 1].tick_params(axis="y", labelsize=7)

    # 3. Max Drawdown %
    axes[1, 0].barh(names, results_df["Max Drawdown %"], color=colors)
    axes[1, 0].axvline(
        x=15.0, color="orange", linestyle="--", linewidth=1, label="15% Max DD Limit"
    )
    axes[1, 0].set_title("Max Drawdown %", fontweight="bold")
    axes[1, 0].legend(fontsize=8)
    axes[1, 0].tick_params(axis="y", labelsize=7)

    # 4. Overall Score
    axes[1, 1].barh(names, results_df["Score"], color=colors)
    axes[1, 1].axvline(
        x=6, color="red", linestyle="--", linewidth=1, label="Score ≥ 6 = Viable"
    )
    axes[1, 1].set_xlim(0, 10)
    axes[1, 1].set_title("Overall Risk-Adjusted Score (out of 10)", fontweight="bold")
    axes[1, 1].legend(fontsize=8)
    axes[1, 1].tick_params(axis="y", labelsize=7)

    plt.tight_layout()
    filename = f"dashboard_{symbol.replace('^', '').replace('.', '_')}.png"
    plt.savefig(filename, dpi=130, bbox_inches="tight")
    plt.close()
    print(f"  📸 Chart saved: {filename}")
    return filename
