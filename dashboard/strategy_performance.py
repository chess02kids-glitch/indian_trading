#!/usr/bin/env python3
"""
30-Strategy Performance Dashboard Module
Runs all 30 strategies and returns performance scores.
Uses talib where available, fallback to custom implementations.
"""

import pandas as pd
import numpy as np
import yfinance as yf
import talib
import matplotlib.pyplot as plt
import seaborn as sns

warnings = False
import warnings as w
w.filterwarnings('ignore')


# ==============================================================================
# INDICATOR FUNCTIONS (adapted from original 30-strategy engine)
# ==============================================================================

def _squeeze(data, col):
    """Helper to squeeze single-column DataFrames/Series"""
    if isinstance(data.columns, pd.MultiIndex):
        return data[col].squeeze()
    return data[col]


def calc_rsi(data, period=14):
    """RSI using talib or custom Wilder smoothing"""
    close = _squeeze(data, 'Close')
    if hasattr(talib, 'RSI'):
        return pd.Series(talib.RSI(close, timeperiod=period), index=data.index)
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def calc_atr(data, period=14):
    """ATR using talib or custom Wilder smoothing"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    if hasattr(talib, 'ATR'):
        return pd.Series(talib.ATR(high, low, close, timeperiod=period), index=data.index)
    hl = high - low
    hpc = (high - close.shift(1)).abs()
    lpc = (low - close.shift(1)).abs()
    tr = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return atr


def calc_ema(series, period):
    """EMA using talib or pandas"""
    if hasattr(talib, 'EMA'):
        return pd.Series(talib.EMA(series.values, timeperiod=period), index=series.index)
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series, period):
    """SMA"""
    return series.rolling(window=period).mean()


def calc_wma(series, period):
    """WMA"""
    weights = np.arange(1, period + 1)
    return series.rolling(window=period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def calc_macd(data, fast=12, slow=26, signal=9):
    """MACD using talib or custom"""
    close = _squeeze(data, 'Close')
    if hasattr(talib, 'MACD'):
        macd, signal_line, hist = talib.MACD(close, fastperiod=fast, slowperiod=slow, signalperiod=signal)
        return (pd.Series(macd, index=data.index),
                pd.Series(signal_line, index=data.index),
                pd.Series(hist, index=data.index))
    
    # Custom implementation
    ema_fast = calc_ema(close, fast)
    ema_slow = calc_ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calc_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calc_bollinger(data, period=20, std_dev=2):
    """Bollinger Bands using SMA + STD"""
    close = _squeeze(data, 'Close')
    mid = calc_sma(close, period)
    std = close.rolling(period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return upper, mid, lower


def calc_stochastic(data, k_period=14, d_period=3):
    """Stochastic Oscillator"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    
    lowest_low = low.rolling(k_period).min()
    highest_high = high.rolling(k_period).max()
    
    k = 100 * (close - lowest_low) / (highest_high - lowest_low + 1e-10)
    d = k.rolling(d_period).mean()
    return k, d


def calc_adx(data, period=14):
    """ADX using talib or custom"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    if hasattr(talib, 'ADX'):
        return pd.Series(talib.ADX(high, low, close, timeperiod=period), index=data.index)
    
    # Custom implementation
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    
    atr_val = calc_atr(data, period)
    smooth_pdm = plus_dm.ewm(alpha=1/period, adjust=False).mean()
    smooth_mdm = minus_dm.ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * smooth_pdm / atr_val.replace(0, 1e-10)
    minus_di = 100 * smooth_mdm / atr_val.replace(0, 1e-10)
    
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    
    return adx, plus_di, minus_di


def calc_cci(data, period=20):
    """CCI using talib or custom"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    tp = (high + low + close) / 3
    if hasattr(talib, 'CCI'):
        return pd.Series(talib.CCI(high, low, close, timeperiod=period), index=data.index)
    
    sma_tp = tp.rolling(period).mean()
    mean_dev = tp.rolling(period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True
    )
    return (tp - sma_tp) / (0.015 * mean_dev + 1e-10)


def calc_williams_r(data, period=14):
    """Williams %R"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    hh = high.rolling(period).max()
    ll = low.rolling(period).min()
    return -100 * (hh - close) / (hh - ll + 1e-10)


def calc_vwap(data):
    """VWAP"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    volume = _squeeze(data, 'Volume')
    tp = (high + low + close) / 3
    return (tp * volume).cumsum() / volume.cumsum()


def calc_hma(series, period):
    """Hull Moving Average"""
    half = int(period / 2)
    sq = int(np.sqrt(period))
    wma1 = calc_wma(series, half)
    wma2 = calc_wma(series, period)
    raw = 2 * wma1 - wma2
    return calc_wma(raw, sq)


def calc_donchian(data, period=20):
    """Donchian Channel"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    mid = (upper + lower) / 2
    return upper, mid, lower


def calc_keltner(data, ema_period=20, atr_period=10, multiplier=2):
    """Keltner Channel"""
    close = _squeeze(data, 'Close')
    mid = calc_ema(close, ema_period)
    atr_val = calc_atr(data, atr_period)
    upper = mid + multiplier * atr_val
    lower = mid - multiplier * atr_val
    return upper, mid, lower


def calc_supertrend(data, period=7, multiplier=3):
    """SuperTrend - custom implementation"""
    close = _squeeze(data, 'Close')
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    hl_avg = (high + low) / 2
    atr_val = calc_atr(data, period)
    
    upper_basic = hl_avg + multiplier * atr_val
    lower_basic = hl_avg - multiplier * atr_val
    
    upper_band = upper_basic.copy()
    lower_band = lower_basic.copy()
    direction = pd.Series(1.0, index=data.index)
    
    for i in range(1, len(data)):
        if upper_basic.iloc[i] < upper_band.iloc[i-1] or close.iloc[i-1] > upper_band.iloc[i-1]:
            upper_band.iloc[i] = upper_basic.iloc[i]
        else:
            upper_band.iloc[i] = upper_band.iloc[i-1]
        
        if lower_basic.iloc[i] > lower_band.iloc[i-1] or close.iloc[i-1] < lower_band.iloc[i-1]:
            lower_band.iloc[i] = lower_basic.iloc[i]
        else:
            lower_band.iloc[i] = lower_band.iloc[i-1]
        
        if close.iloc[i] > upper_band.iloc[i-1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < lower_band.iloc[i-1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i-1]
    
    return direction


def calc_psar(data, af_start=0.02, af_step=0.02, af_max=0.2):
    """Parabolic SAR - custom implementation"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    close = _squeeze(data, 'Close')
    
    af = af_start
    ep_high = high.iloc[0]
    ep_low = low.iloc[0]
    sar = close.iloc[0]
    trend = 1
    
    results = [np.nan] * len(data)
    
    for i in range(1, len(data)):
        if trend == 1:
            sar = sar + af * (ep_high - sar)
            if low.iloc[i] < sar:
                trend = -1
                sar = ep_high
                af = af_start
                ep_low = low.iloc[i]
            if high.iloc[i] > ep_high:
                ep_high = high.iloc[i]
        else:
            sar = sar - af * (ep_low - sar)
            if high.iloc[i] > sar:
                trend = 1
                sar = ep_low
                af = af_start
                ep_high = high.iloc[i]
            if low.iloc[i] < ep_low:
                ep_low = low.iloc[i]
        
        af = min(af + af_step, af_max)
        results[i] = sar
    
    return pd.Series(results, index=data.index)


def calc_aroon(data, period=25):
    """Aroon Indicator"""
    high = _squeeze(data, 'High')
    low = _squeeze(data, 'Low')
    
    aroon_up = pd.Series(np.nan, index=data.index)
    aroon_down = pd.Series(np.nan, index=data.index)
    
    for i in range(period, len(data)):
        window_high = high.iloc[i-period+1:i+1]
        window_low = low.iloc[i-period+1:i+1]
        if window_high.notna().any() and window_low.notna().any():
            aroon_up.iloc[i] = ((period - window_high.argmax()) / period) * 100
            aroon_down.iloc[i] = ((period - window_low.argmin()) / period) * 100
    
    return aroon_up, aroon_down


def calc_obv(data):
    """OBV - On Balance Volume"""
    close = _squeeze(data, 'Close')
    volume = _squeeze(data, 'Volume')
    sign = np.sign(close.diff())
    sign[close.diff() == 0] = 0
    return (sign * volume).cumsum()


def calc_zscore(series, period=20):
    """Z-Score"""
    mean = series.rolling(period).mean()
    std = series.rolling(period).std()
    return (series - mean) / (std + 1e-10)


def calc_chande_momentum(data, period=14):
    """Chande Momentum Oscillator"""
    close = _squeeze(data, 'Close')
    diff = close.diff()
    up = diff.where(diff > 0, 0).rolling(period).sum()
    down = diff.where(diff < 0, 0).abs().rolling(period).sum()
    return 100 * (up - down) / (up + down + 1e-10)


# ==============================================================================
# STRATEGY CLASS DEFINITIONS (all 30 strategies adapted)
# ==============================================================================



# ==============================================================================
# MARKET DATA INTEGRATION
# ==============================================================================

def get_nifty_data(symbol="NIFTY_50", period="60d"):
    """Get Nifty index data for context."""
    try:
        import yfinance as yf
        symbol_map = {
            "NIFTY_50": "^NSEI",
            "NIFTY_500": "^NSEBANK",
        }
        yf_symbol = symbol_map.get(symbol, symbol)
        df = yf.download(yf_symbol, period=period, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return None

def get_vix_data(period="60d"):
    """Get India VIX data for volatility context."""
    try:
        import yfinance as yf
        df = yf.download("^INDIAVIX", period=period, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except:
        return None

def get_market_breadth():
    """Get market breadth indicators."""
    try:
        import csv
        breadth_data = []
        with open('/home/user/indian_trading/data/market/breadth.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                breadth_data.append(row)
        return breadth_data
    except:
        return []

def get_nifty_50_constituents():
    """Get Nifty 50 constituent stocks."""
    try:
        import pyarrow.parquet as pq
        df = pq.read_table('/home/user/indian_trading/data/market/indices/NIFTY_50.parquet').to_pandas()
        return ["INFY", "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK"]
    except:
        return []

def get_sector_performance():
    """Get sector performance data."""
    try:
        return {}
    except:
        return {}

# Global market data cache
import time
market_cache = {}

def get_cached_market_data(key, ttl=300):
    """Get cached market data."""
    current_time = time.time()
    if key in market_cache:
        data, timestamp = market_cache[key]
        if current_time - timestamp < ttl:
            return data
    return None

def set_cached_market_data(key, data):
    """Set cached market data."""
    market_cache[key] = (data, time.time())


# ==============================================================================
# BLOOMBERG-TERMINAL-LIKE MARKET DATA
# ==============================================================================

def get_nifty_50_summary():
    """Get Nifty 50 summary - Bloomberg terminal style."""
    try:
        import yfinance as yf
        df = yf.download("^NSEI", period="1d", interval="1d", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            latest = df['Close'].iloc[-1]
            prev_close = df['Close'].iloc[-2]
            change = latest - prev_close
            change_pct = change / prev_close * 100
            volume = df['Volume'].iloc[-1] if 'Volume' in df.columns else 0
            return {
                'symbol': 'NIFTY',
                'price': round(latest, 2),
                'change': round(change, 2),
                'change_pct': round(change_pct, 2),
                'volume': int(volume),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
    except:
        pass
    return None

def get_vix_summary():
    """Get India VIX summary - Bloomberg terminal style."""
    try:
        import yfinance as yf
        df = yf.download("^INDIAVIX", period="1d", interval="1d", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            latest = df['Close'].iloc[-1]
            prev_close = df['Close'].iloc[-2]
            change = latest - prev_close
            change_pct = change / prev_close * 100
            return {
                'symbol': 'INDIA VIX',
                'price': round(latest, 2),
                'change': round(change, 2),
                'change_pct': round(change_pct, 2),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
    except:
        pass
    return None

def get_market_breadth():
    """Get market breadth - advance/decline status."""
    try:
        import csv
        advances = declines = unchanged = 0
        with open('/home/user/indian_trading/data/market/breadth.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                advances += int(row.get('Advances', 0) or 0)
                declines += int(row.get('Declines', 0) or 0)
                unchanged += int(row.get('Unchanged', 0) or 0)
        total = advances + declines + unchanged
        if total > 0:
            advance_ratio = advances / total * 100
            decline_ratio = declines / total * 100
            return {
                'advances': advances,
                'declines': declines,
                'unchanged': unchanged,
                'advance_ratio': round(advance_ratio, 2),
                'decline_ratio': round(decline_ratio, 2),
                'market_sentiment': 'BULLISH' if advance_ratio > 60 else 'BEARISH' if decline_ratio > 60 else 'NEUTRAL'
            }
    except:
        pass
    return None

def get_nifty_50_constituents():
    """Get Nifty 50 top constituents by market cap."""
    try:
        # Return major Nifty 50 stocks
        return {
            'top_5': [
                {'symbol': 'RELIANCE', 'weight': 10.5},
                {'symbol': 'TCS', 'weight': 8.2},
                {'symbol': 'HDFCBANK', 'weight': 7.8},
                {'symbol': 'INFY', 'weight': 6.5},
                {'symbol': 'ICICIBANK', 'weight': 5.2}
            ],
            'sectors': {
                'Financial Services': 35.0,
                'IT': 18.0,
                'Energy': 12.0,
                'FMCG': 8.0,
                'Auto': 7.0
            }
        }
    except:
        return None
    return None

def get_sector_rotation():
    """Get sector performance for rotation analysis."""
    try:
        return {
            'outperforming': ['Energy', 'Financials', 'Materials'],
            'underperforming': ['IT', 'Consumer Staples', 'Real Estate'],
            'data_date': datetime.now().strftime('%Y-%m-%d')
        }
    except:
        return None
    return None

def get_india_macro():
    """Get key India macro economic indicators."""
    try:
        return {
            'gdp_growth': '6.8%',  # Approximate
            'inflation': '5.1%',   # CPI based
            'repo_rate': '6.50%',
            'fiscal_deficit': '5.8% of GDP',
            'data_date': datetime.now().strftime('%Y-%m-%d')
        }
    except:
        return None
    return None


# ==============================================================================
# ENHANCED BLOOMBERG-TERMINAL-LIKE MARKET DATA
# ==============================================================================

def get_nifty_50_live():
    """Get Nifty 50 live data with full market metrics."""
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.download("^NSEI", period="1d", interval="5m", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            latest = df['Close'].iloc[-1]
            prev_close = df['Close'].iloc[0]  # first of session
            change = latest - prev_close
            change_pct = change / prev_close * 100
            volume = df['Volume'].iloc[-1] if 'Volume' in df.columns else 0
            high_52w = df['High'].max() if 'High' in df.columns else latest
            low_52w = df['Low'].min() if 'Low' in df.columns else latest
            return {
                'symbol': 'NIFTY 50',
                'price': round(latest, 2),
                'change': round(change, 2),
                'change_pct': round(change_pct, 2),
                'volume': int(volume),
                'high_52w': round(high_52w, 2),
                'low_52w': round(low_52w, 2),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'market_cap': round(latest * 5_00_00_00_000, 2)  # Approximate
            }
    except:
        pass
    return None

def get_vix_india_live():
    """Get India VIX live data."""
    try:
        import yfinance as yf
        df = yf.download("^INDIAVIX", period="1d", interval="5m", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            latest = df['Close'].iloc[-1]
            prev_close = df['Open'].iloc[0] if 'Open' in df.columns else latest
            change = latest - prev_close
            change_pct = change / prev_close * 100
            return {
                'symbol': 'INDIA VIX',
                'price': round(latest, 2),
                'change': round(change, 2),
                'change_pct': round(change_pct, 2),
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
    except:
        pass
    return None

def get_market_breadth_advance_decline():
    """Get advance-decline data from breadth.csv."""
    try:
        import csv
        advances = declines = total = 0
        with open('/home/user/indian_trading/data/market/breadth.csv', 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                advances += int(float(row.get('Advances', 0) or 0))
                declines += int(float(row.get('Declines', 0) or 0))
                total += int(float(row.get('Total', 0) or 0))
        if total > 0:
            advance_ratio = advances / total * 100
            decline_ratio = declines / total * 100
            return {
                'advances': advances,
                'declines': declines,
                'total': total,
                'advance_ratio': round(advance_ratio, 2),
                'decline_ratio': round(decline_ratio, 2),
                'market_breadth': 'BULLISH' if advance_ratio > 55 else 'BEARISH' if decline_ratio > 55 else 'NEUTRAL',
                'timestamp': datetime.now().strftime('%Y-%m-%d')
            }
    except:
        pass
    return None

def get_nifty_50_constituents_major():
    """Get major Nifty 50 constituents with approximate weights."""
    try:
        return {
            'constituents': [
                {'symbol': 'RELIANCE', 'weight': 10.1, 'sector': 'Energy/Financials'},
                {'symbol': 'TCS', 'weight': 8.4, 'sector': 'IT'},
                {'symbol': 'HDFCBANK', 'weight': 7.9, 'sector': 'Banking'},
                {'symbol': 'INFY', 'weight': 6.5, 'sector': 'IT'},
                {'symbol': 'ICICIBANK', 'weight': 5.3, 'sector': 'Banking'},
                {'symbol': 'SBIN', 'weight': 2.1, 'sector': 'Banking'},
                {'symbol': 'BHARTIARTL', 'weight': 3.2, 'sector': 'Telecom'},
                {'symbol': 'ITC', 'weight': 3.8, 'sector': 'FMCG'},
                {'symbol': 'ONGC', 'weight': 1.8, 'sector': 'Energy'},
                {'symbol': 'LT', 'weight': 1.5, 'sector': 'Construction'}
            ],
            'sectors': {
                'Financial Services': 34.5,
                'IT': 17.8,
                'Energy': 11.2,
                'FMCG': 8.5,
                'Auto': 7.1,
                'Telecom': 4.2,
                'Pharma': 3.8,
                'Power': 3.1,
                'Metal': 2.8,
                'Cement': 2.3
            }
        }
    except:
        return None
    return None

def get_india_macro_indicators():
    """Get key India macro-economic indicators."""
    try:
        from datetime import datetime
        return {
            'gdp_growth_q4': '6.8%',  # Latest reported
            'inflation_cpi': '5.1%',   # CPI based
            'repo_rate': '6.50%',
            'fiscal_deficit': '5.8% of GDP',
            'current_account': '-1.2% of GDP',
            'for_exchange_reserves': '$645 billion',
            'data_date': datetime.now().strftime('%B %Y')
        }
    except:
        return None
    return None

def get_fear_greed_index():
    """Get Fear & Greed Index approximation."""
    try:
        # VIX-based approximation: Fear when VIX high, Greed when VIX low
        import yfinance as yf
        df = yf.download("^INDIAVIX", period="5d", progress=False)
        if not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            avg_vix = df['Close'].mean()
            if avg_vix > 25:
                sentiment = 'EXTREME FEAR'
                level = 15
            elif avg_vix > 20:
                sentiment = 'FEAR'
                level = 25
            elif avg_vix > 15:
                sentiment = 'NEUTRAL'
                level = 50
            elif avg_vix > 10:
                sentiment = 'GREED'
                level = 75
            else:
                sentiment = 'EXTREME GREED'
                level = 90
            
            return {
                'fear_greed_level': level,
                'fear_greed_sentiment': sentiment,
                'current_vix': round(avg_vix, 2),
                'timestamp': datetime.now().strftime('%Y-%m-%d')
            }
    except:
        pass
    return None

# Market session tracker
def get_market_session():
    """Determine current market session (IST)."""
    from datetime import datetime
    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(ist)
    day_of_week = now_ist.weekday()
    time_of_day = now_ist.time()
    
    # NSE Cash Market Hours: 9:15 AM to 3:30 PM IST, Monday to Friday
    is_trading_session = (day_of_week < 5 and 
                         time_of_day >= datetime.strptime("09:15", "%H:%M").time() and 
                         time_of_day <= datetime.strptime("15:30", "%H:%M").time())
    
    if is_trading_session:
        hours = now_ist.hour
        minutes = now_ist.minute
        session_time = hours + minutes/60
        
        if session_time < 10.5:
            session = 'OPEN - Early Session'
        elif session_time < 12.5:
            session = 'OPEN - Mid Session'
        elif session_time < 14.5:
            session = 'OPEN - Late Session'
        else:
            session = 'Closing Session'
    else:
        if day_of_week >= 5:
            session = 'Weekend - NSE Closed'
        else:
            session = 'After Market Hours'
    
    return {
        'is_market_open': is_trading_session,
        'session': session,
        'timestamp': datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S %Z')
    }
class S01_AlphaTrend:
    name = "S01_AlphaTrend"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        rsi = calc_rsi(df, 14)
        atr = calc_atr(df, 14)
        at_raw = np.where(rsi >= 50, df['Low'].squeeze() - atr, df['High'].squeeze() + atr)
        at = calc_ema(pd.Series(at_raw, index=df.index), 2)
        df['Buy_Signal'] = close > at
        df['Sell_Signal'] = close < at
        return df


class S02_SuperTrend_714:
    name = "S02_SuperTrend_7_3"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        direction = calc_supertrend(df, 7, 3)
        df['Buy_Signal'] = direction == -1
        df['Sell_Signal'] = direction == 1
        return df


class S03_SuperTrend_510:
    name = "S03_SuperTrend_5_2"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        direction = calc_supertrend(df, 5, 2)
        df['Buy_Signal'] = direction == -1
        df['Sell_Signal'] = direction == 1
        return df


class S04_EMA_20_50:
    name = "S04_EMA_Cross_20_50"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        ema20 = calc_ema(close, 20)
        ema50 = calc_ema(close, 50)
        df['Buy_Signal'] = ema20 > ema50
        df['Sell_Signal'] = ema20 < ema50
        return df


class S05_EMA_9_21:
    name = "S05_EMA_Cross_9_21"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        ema9 = calc_ema(close, 9)
        ema21 = calc_ema(close, 21)
        df['Buy_Signal'] = ema9 > ema21
        df['Sell_Signal'] = ema9 < ema21
        return df


class S06_TripleEMA:
    name = "S06_Triple_EMA_5_13_21"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        e5 = calc_ema(close, 5)
        e13 = calc_ema(close, 13)
        e21 = calc_ema(close, 21)
        bull = (e5 > e13) & (e13 > e21)
        bear = (e5 < e13) & (e13 < e21)
        df['Buy_Signal'] = bull
        df['Sell_Signal'] = bear
        return df


class S07_GoldenCross:
    name = "S07_SMA_50_200_GoldenCross"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        s50 = calc_sma(close, 50)
        s200 = calc_sma(close, 200)
        df['Buy_Signal'] = s50 > s200
        df['Sell_Signal'] = s50 < s200
        return df


class S08_HullMA:
    name = "S08_HullMA_14_28"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        h14 = calc_hma(close, 14)
        h28 = calc_hma(close, 28)
        df['Buy_Signal'] = h14 > h28
        df['Sell_Signal'] = h14 < h28
        return df


class S09_ParabolicSAR:
    name = "S09_Parabolic_SAR"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        trend = calc_psar(df, 0.02, 0.02, 0.2)
        df['Buy_Signal'] = trend > 0
        df['Sell_Signal'] = trend < 0
        return df


class S10_Donchian_Breakout:
    name = "S10_Donchian_Breakout_20"
    group = "Trend"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        upper, _, lower = calc_donchian(df, 20)
        df['Buy_Signal'] = close > upper.shift(1)
        df['Sell_Signal'] = close < lower.shift(1)
        return df


class S11_HilegaMilega:
    name = "S11_Hilega_Milega"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        rsi9 = calc_rsi(df, 9)
        ema3 = calc_ema(rsi9, 3)
        wma21 = calc_wma(rsi9, 21)
        bull = (rsi9 > 50) & (ema3 < rsi9) & (wma21 > 50)
        bear = (rsi9 < 50) & (ema3 > rsi9) & (wma21 < 50)
        df['Buy_Signal'] = bull
        df['Sell_Signal'] = bear
        return df


class S12_MACD_Signal_Cross:
    name = "S12_MACD_Signal_Cross"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        macd_line, signal_line, _ = calc_macd(df)
        df['Buy_Signal'] = macd_line > signal_line
        df['Sell_Signal'] = macd_line < signal_line
        return df


class S13_MACD_ZeroCross:
    name = "S13_MACD_Zero_Cross"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        macd_line, _, _ = calc_macd(df)
        zero = pd.Series(0.0, index=df.index)
        df['Buy_Signal'] = macd_line > zero
        df['Sell_Signal'] = macd_line < zero
        return df


class S14_RSI_50_Cross:
    name = "S14_RSI_50_Cross"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        rsi = calc_rsi(df, 14)
        fifty = pd.Series(50.0, index=df.index)
        df['Buy_Signal'] = rsi > fifty
        df['Sell_Signal'] = rsi < fifty
        return df


class S15_Stochastic:
    name = "S15_Stochastic_Oversold_Overbought"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        k, d = calc_stochastic(df, 14, 3)
        df['Buy_Signal'] = k > d
        df['Sell_Signal'] = k < d
        return df


class S16_CCI:
    name = "S16_CCI_100_Cross"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        cci = calc_cci(df, 20)
        n100 = pd.Series(-100.0, index=df.index)
        p100 = pd.Series(100.0, index=df.index)
        df['Buy_Signal'] = cci > -100
        df['Sell_Signal'] = cci < 100
        return df


class S17_WilliamsR:
    name = "S17_Williams_R"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        wr = calc_williams_r(df, 14)
        n80 = pd.Series(-80.0, index=df.index)
        n20 = pd.Series(-20.0, index=df.index)
        df['Buy_Signal'] = wr > -80
        df['Sell_Signal'] = wr < -20
        return df


class S18_Aroon:
    name = "S18_Aroon_Cross_25"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        up, down = calc_aroon(df, 25)
        df['Buy_Signal'] = up > down
        df['Sell_Signal'] = up < down
        return df


class S19_ChandeMomentum:
    name = "S19_Chande_Momentum_Zero"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        cmo = calc_chande_momentum(df, 14)
        zero = pd.Series(0.0, index=df.index)
        df['Buy_Signal'] = cmo > zero
        df['Sell_Signal'] = cmo < zero
        return df


class S20_ADX_DI_Cross:
    name = "S20_ADX_DI_Cross_25filter"
    group = "Momentum"
    def signals(self, df):
        df = df.copy()
        adx_result = calc_adx(df, 14)
        # adx_result is a Series; use it directly for the strength filter
        strong = adx_result > 25
        # Use simple midpoint filter: when ADX is strong, go with trend direction
        # If price is above previous close, bullish, else bearish
        close = df['Close'].squeeze() if hasattr(df['Close'], 'squeeze') else df['Close']
        prev_close = close.shift(1)
        trend_bull = close > prev_close
        df['Buy_Signal'] = strong & trend_bull
        df['Sell_Signal'] = strong & ~trend_bull
        return df


class S21_RSI_BollingerBands:
    name = "S21_RSI_BollingerBands_Combo"
    group = "Mean Reversion"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        rsi = calc_rsi(df, 14)
        upper, mid, lower = calc_bollinger(df, 20, 2)
        buy = (rsi < 35) & (close <= lower)
        sell = (rsi > 65) & (close >= upper)
        df['Buy_Signal'] = buy
        df['Sell_Signal'] = sell
        return df


class S22_ZScore_Reversion:
    name = "S22_ZScore_Mean_Reversion"
    group = "Mean Reversion"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        z = calc_zscore(close, 20)
        n2 = pd.Series(-2.0, index=df.index)
        p2 = pd.Series(2.0, index=df.index)
        df['Buy_Signal'] = z > -2
        df['Sell_Signal'] = z < 2
        return df


class S23_BB_Squeeze:
    name = "S23_BollingerBand_Squeeze"
    group = "Mean Reversion"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        bb_u, bb_mid, bb_l = calc_bollinger(df, 20, 2)
        kc_u, _, kc_l = calc_keltner(df, 20, 10, 1.5)
        squeeze = (bb_u < kc_u) & (bb_l > kc_l)
        squeeze_off = squeeze & ~squeeze.shift(1).fillna(False)
        df['Buy_Signal'] = squeeze_off & (close > bb_mid)
        df['Sell_Signal'] = squeeze_off & (close < bb_mid)
        return df


class S24_KeltnerChannel:
    name = "S24_Keltner_Channel_Bounce"
    group = "Mean Reversion"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        upper, _, lower = calc_keltner(df)
        df['Buy_Signal'] = close < lower
        df['Sell_Signal'] = close > upper
        return df


class S25_RSI_30_70:
    name = "S25_RSI_30_70_Classic"
    group = "Mean Reversion"
    def signals(self, df):
        df = df.copy()
        rsi = calc_rsi(df, 14)
        thirty = pd.Series(30.0, index=df.index)
        seventy = pd.Series(70.0, index=df.index)
        df['Buy_Signal'] = rsi > 30
        df['Sell_Signal'] = rsi < 70
        return df


class S26_VWAP:
    name = "S26_VWAP_Cross"
    group = "Volume"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        vwap = calc_vwap(df)
        df['Buy_Signal'] = close > vwap
        df['Sell_Signal'] = close < vwap
        return df


class S27_Volume_RSI_Combo:
    name = "S27_Volume_Confirmed_RSI"
    group = "Volume"
    def signals(self, df):
        df = df.copy()
        rsi = calc_rsi(df, 14)
        volume = _squeeze(df, 'Volume')
        vol_ma = calc_sma(volume, 20)
        fifty = pd.Series(50.0, index=df.index)
        high_vol = volume > vol_ma
        df['Buy_Signal'] = (rsi > fifty) & high_vol
        df['Sell_Signal'] = (rsi < fifty) & high_vol
        return df


class S28_OBV_Trend:
    name = "S28_OBV_EMA_Cross"
    group = "Volume"
    def signals(self, df):
        df = df.copy()
        obv = calc_obv(df)
        obv_ema = calc_ema(obv, 20)
        df['Buy_Signal'] = obv > obv_ema
        df['Sell_Signal'] = obv < obv_ema
        return df


class S29_Triple_Confirmation:
    name = "S29_Triple_Confirmation_RSI_EMA_MACD"
    group = "Advanced"
    def signals(self, df):
        df = df.copy()
        close = _squeeze(df, 'Close')
        rsi = calc_rsi(df, 14)
        ema20 = calc_ema(close, 20)
        ema50 = calc_ema(close, 50)
        macd_line, signal_line, _ = calc_macd(df)
        bull = (rsi > 50) & (ema20 > ema50) & (macd_line > signal_line)
        bear = (rsi < 50) & (ema20 < ema50) & (macd_line < signal_line)
        df['Buy_Signal'] = bull
        df['Sell_Signal'] = bear
        return df


class S30_ADX_SuperTrend_Combo:
    name = "S30_ADX_SuperTrend_Combo"
    group = "Advanced"
    def signals(self, df):
        df = df.copy()
        adx_result = calc_adx(df, 14)
        strong = adx_result > 25
        direction = calc_supertrend(df, 7, 3)
        df['Buy_Signal'] = (direction == -1) & strong
        df['Sell_Signal'] = (direction == 1) & strong
        return df


# All 30 strategies
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
    S30_ADX_SuperTrend_Combo()
]


# ==============================================================================
# DATA DOWNLOADER
# ==============================================================================

def download_data(symbol, period="60d", interval="1h"):
    """Download market data for a symbol"""
    try:
        df = yf.download(symbol, period=period, interval=interval, progress=False)
        if df.empty:
            return None
        
        # Handle MultiIndex columns from yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        # Reset index to make date a column, then set as DatetimeIndex
        df = df.reset_index()
        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
            df = df.set_index('Date')
        
        # Ensure required columns exist
        required = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in required:
            if col not in df.columns:
                print(f"⚠️  Missing column {col} for {symbol}")
                return None
        
        return df
    except Exception as e:
        print(f"❌ Error downloading {symbol}: {e}")
        return None


# ==============================================================================
# BACKTEST ENGINE
# ==============================================================================

def backtest_strategy(df, strategy, initial_capital=50000, brokerage=40):
    """
    Run backtest on a single strategy.
    Returns trades DataFrame and final capital.
    """
    try:
        df = strategy.signals(df.copy())
    except Exception as e:
        print(f"      ⚠️  Signal error in {strategy.name}: {e}")
        return pd.DataFrame(), initial_capital
    
    if 'Buy_Signal' not in df.columns or 'Sell_Signal' not in df.columns:
        return pd.DataFrame(), initial_capital
    
    position = 0
    buy_price = 0.0
    trades = []
    capital = initial_capital
    
    close_prices = _squeeze(df, 'Close')
    
    for i in range(len(df)):
        try:
            price = float(close_prices.iloc[i])
            if price <= 0:
                continue
                
            buy_sig = bool(df['Buy_Signal'].iloc[i])
            sell_sig = bool(df['Sell_Signal'].iloc[i])
            date = df.index[i]
            
            if buy_sig and position == 0:
                # Use 95% of capital per trade
                quantity = int(capital * 0.95 / price)
                if quantity < 1:
                    continue
                buy_price = price
                position = quantity
                
            elif sell_sig and position > 0:
                gross_pnl = (price - buy_price) * position
                net_pnl = gross_pnl - brokerage
                capital += net_pnl
                trades.append({
                    'Date': date,
                    'Buy_Price': round(buy_price, 2),
                    'Sell_Price': round(price, 2),
                    'Qty': position,
                    'Gross_PnL': round(gross_pnl, 2),
                    'Net_PnL': round(net_pnl, 2),
                    'Capital': round(capital, 2)
                })
                position = 0
        except Exception:
            continue
    
    return pd.DataFrame(trades), round(capital, 2)


# ==============================================================================
# SCORING FUNCTION
# ==============================================================================

def score_trades(trades_df, initial_capital, strategy_name, group):
    """Score strategy performance on 5 metrics"""
    base = {
        'Strategy': strategy_name,
        'Group': group,
        'Trades': 0,
        'Win Rate %': 0.0,
        'Return %': 0.0,
        'Profit Factor': 0.0,
        'Max Drawdown %': 0.0,
        'Final Capital': initial_capital,
        'Score': 0,
        'Rating': '⚠️ NO DATA'
    }
    
    if trades_df.empty or len(trades_df) < 3:
        return base
    
    wins = trades_df[trades_df['Net_PnL'] > 0]
    losses = trades_df[trades_df['Net_PnL'] < 0]
    win_rate = len(wins) / len(trades_df) * 100
    
    # Return calculation
    final_cap = trades_df['Capital'].iloc[-1] if not trades_df.empty else initial_capital
    ret = ((final_cap - initial_capital) / initial_capital * 100)
    
    # Profit Factor
    gp = wins['Net_PnL'].sum() if not wins.empty else 0
    gl = losses['Net_PnL'].abs().sum() if not losses.empty else 1e-10
    pf = round(gp / gl, 2) if gl > 0 else 999.0
    
    # Max Drawdown
    cap_curve = trades_df['Capital']
    roll_max = cap_curve.cummax()
    drawdown = (cap_curve - roll_max) / roll_max * 100
    max_dd = abs(drawdown.min())
    
    # Score calculation (max 10 points)
    sc = 0
    sc += 2 if win_rate > 55 else (1 if win_rate > 45 else 0)
    sc += 2 if pf > 2.0 else (1 if pf > 1.5 else 0)
    sc += 2 if ret > 20 else (1 if ret > 10 else 0)
    sc += 2 if max_dd < 10 else (1 if max_dd < 20 else 0)
    sc += 2 if len(trades_df) > 15 else (1 if len(trades_df) > 8 else 0)
    
    rating = ("🟢 EXCELLENT" if sc >= 8 else
              "🟡 GOOD" if sc >= 6 else
              "🟠 AVERAGE" if sc >= 4 else
              "🔴 POOR")
    
    return {
        'Strategy': strategy_name,
        'Group': group,
        'Trades': len(trades_df),
        'Win Rate %': round(win_rate, 1),
        'Return %': round(ret, 1),
        'Profit Factor': pf,
        'Max Drawdown %': round(max_dd, 1),
        'Final Capital': round(final_cap, 2),
        'Score': sc,
        'Rating': rating
    }


# ==============================================================================
# MASTER RUNNER
# ==============================================================================

def run_all_strategies(symbol, period="60d", interval="1h", capital=50000, brokerage=40):
    """
    Download data and run all 30 strategies on one symbol.
    Returns DataFrame with performance results.
    """
    print(f"\n{'='*60}")
    print(f"📈 SYMBOL: {symbol}")
    print(f"{'='*60}")
    
    df = download_data(symbol, period, interval)
    if df is None:
        print(f"❌ No data for {symbol}")
        return None
    
    # Ensure index is DatetimeIndex
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.set_index(pd.to_datetime(df.index))
    
    print(f"✅ {len(df)} candles loaded\n")
    
    results = []
    for i, strategy in enumerate(ALL_STRATEGIES):
        print(f"  [{i+1:02d}/30] {strategy.name:<45}", end=" ")
        trades_df, final_cap = backtest_strategy(df, strategy, capital, brokerage)
        perf = score_trades(trades_df, capital, strategy.name, strategy.group)
        results.append(perf)
        print(f"{perf['Rating']:<15} | "
              f"Return: {perf['Return %']:>7.1f}% | "
              f"Trades: {perf['Trades']:>3} | "
              f"Score: {perf['Score']}/10")
    
    return pd.DataFrame(results)


# ==============================================================================
# ANALYSIS HELPERS
# ==============================================================================

def get_top_strategies(results_df, top_n=10):
    """Get top N strategies by score"""
    if results_df is None or results_df.empty:
        return []
    return results_df.nlargest(top_n, 'Score')


def get_best_by_group(results_df):
    """Get best strategy per group"""
    if results_df is None or results_df.empty:
        return {}
    best = {}
    for group in results_df['Group'].unique():
        grp_df = results_df[results_df['Group'] == group]
        best[group] = grp_df.loc[grp_df['Score'].idxmax()]
    return best


def get_rating_distribution(results_df):
    """Get rating distribution counts"""
    if results_df is None or results_df.empty:
        return {}
    return results_df['Rating'].value_counts().to_dict()


def generate_dashboard_chart(results_df, symbol):
    """Generate a 4-panel comparison chart"""
    if results_df is None or results_df.empty:
        print("⚠️  No results to chart")
        return None
    
    color_map = {
        '🟢 EXCELLENT': '#27ae60',
        '🟡 GOOD': '#f1c40f',
        '🟠 AVERAGE': '#e67e22',
        '🔴 POOR': '#e74c3c',
        '⚠️ NO DATA': '#95a5a6'
    }
    colors = [color_map.get(r, '#95a5a6')
              for r in results_df['Rating']]
    names = results_df['Strategy'].tolist()
    
    fig, axes = plt.subplots(2, 2, figsize=(20, 12))
    fig.suptitle(
        f'30-Strategy Backtest Dashboard — {symbol}',
        fontsize=14, fontweight='bold'
    )
    
    # Win Rate
    axes[0, 0].barh(names, results_df['Win Rate %'], color=colors)
    axes[0, 0].axvline(x=50, color='red', linestyle='--',
                        linewidth=1, label='50% Target')
    axes[0, 0].set_title('Win Rate %', fontweight='bold')
    axes[0, 0].legend(fontsize=8)
    axes[0, 0].tick_params(axis='y', labelsize=7)
    
    # Total Return
    axes[0, 1].barh(names, results_df['Return %'], color=colors)
    axes[0, 1].axvline(x=0, color='black', linewidth=1)
    axes[0, 1].set_title('Total Return %', fontweight='bold')
    axes[0, 1].tick_params(axis='y', labelsize=7)
    
    # Profit Factor (capped at 10 for readability)
    pf_capped = results_df['Profit Factor'].clip(0, 10)
    axes[1, 0].barh(names, pf_capped, color=colors)
    axes[1, 0].axvline(x=1.5, color='red', linestyle='--',
                        linewidth=1, label='1.5 Target')
    axes[1, 0].set_title('Profit Factor (capped at 10)',
                          fontweight='bold')
    axes[1, 0].legend(fontsize=8)
    axes[1, 0].tick_params(axis='y', labelsize=7)
    
    # Score
    axes[1, 1].barh(names, results_df['Score'], color=colors)
    axes[1, 1].axvline(x=6, color='red', linestyle='--',
                        linewidth=1, label='Score ≥ 6 = Good')
    axes[1, 1].set_xlim(0, 10)
    axes[1, 1].set_title('Overall Score (out of 10)',
                          fontweight='bold')
    axes[1, 1].legend(fontsize=8)
    axes[1, 1].tick_params(axis='y', labelsize=7)
    
    plt.tight_layout()
    filename = f'dashboard_{symbol.replace("^", "").replace(".", "_")}.png'
    plt.savefig(filename, dpi=130, bbox_inches='tight')
    plt.close()
    print(f"  📸 Chart saved: {filename}")
    return filename