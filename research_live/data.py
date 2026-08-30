"""Data loading for the live research engine.

Loads the clean, split/bonus-adjusted daily OHLCV parquets into a wide
panel (symbol x date). Builds a market index proxy from the panel and
provides universe selection.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "clean", "eod2_data")
INDEX_FILE = os.path.join(
    os.path.dirname(__file__), "..", "data", "eod2", "daily", "nifty 50.csv"
)


def load_panel(columns=("date", "symbol", "open", "high", "low", "close", "volume")):
    """Return a MultiIndex (date, symbol) DataFrame of OHLCV."""
    frames = []
    for f in glob.glob(os.path.join(DATA_DIR, "*.parquet")):
        df = pd.read_parquet(f, columns=list(columns))
        df = df.dropna(subset=["date", "close"])
        df["date"] = pd.to_datetime(df["date"])
        frames.append(df)
    if not frames:
        raise RuntimeError("no clean parquet data found")
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["date", "symbol"], keep="last")
    out = out.sort_values(["date", "symbol"]).set_index(["date", "symbol"])
    # sanity: drop obvious bad prices
    out = out[(out["close"] > 0) & (out["high"] >= out["low"]) & (out["high"] > 0)]
    return out


def to_wide(panel, col="close"):
    """Return symbol x date wide matrix of col."""
    w = panel[col].unstack("symbol")
    w = w.sort_index()
    return w


def market_index(close_wide):
    """Equal-weight, dividend-adjusted-ish market proxy from the liquid universe.

    Rebased so each month all available names are equal weighted; then
    compounded into an index. NaN months drop out of the average.
    """
    ret = close_wide.pct_change()
    m = ret.resample("ME").last().iloc[1:]
    idx = (1.0 + m.mean(axis=1)).cumprod()
    idx.index = idx.index.to_period("M").to_timestamp("M")
    return idx


def liquid_universe(panel, start="2008-01-01", min_frac=0.9):
    """Symbols present for >= min_frac of trading days after start."""
    w = to_wide(panel)
    w = w.loc[w.index >= pd.Timestamp(start)]
    frac = w.notna().mean()
    return frac[frac >= min_frac].index.tolist()


def align_panel(panel, symbols, start="2008-01-01", end="2026-06-30"):
    """Return close_wide and the full OHLCV panel restricted to universe+range."""
    sub = panel.loc[(slice(pd.Timestamp(start), pd.Timestamp(end)), slice(None)), :]
    sub = sub[sub.index.get_level_values("symbol").isin(symbols)]
    close_wide = to_wide(sub)
    return sub, close_wide
