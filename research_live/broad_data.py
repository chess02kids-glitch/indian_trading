"""Build a broad, split-adjusted universe from the daily EOD CSVs.

The clean parquets only cover 133 large caps. Here we load all daily CSVs,
apply backward split adjustment using corporate_actions.csv, and filter to
liquid names with sufficient history.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

DAILY = os.path.join(os.path.dirname(__file__), "..", "data", "eod2", "daily")
CA = os.path.join(os.path.dirname(__file__), "..", "data", "corporate_actions.csv")


def load_splits():
    ca = pd.read_csv(CA)
    ca["date"] = pd.to_datetime(ca["date"])
    ca["symbol"] = ca["symbol"].str.strip().str.upper()
    # total split multiplier = product of splits after date (backward adjustment)
    # split column interpreted as multiplier on shares (e.g. 2 = 2x shares)
    return ca[["date", "symbol", "split"]]


def _read_symbol(path):
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    if "date" not in df.columns or "close" not in df.columns:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"])
    if "series" in df.columns:
        df = df[df["series"] == "EQ"]
    df = df.sort_values("date").drop_duplicates("date")
    return df


def load_broad_universe(min_years=8, min_avg_value=5e6, start="2010-01-01"):
    """Return dict symbol-> DataFrame(date, open, high, low, close, volume, value)."""
    splits = load_splits()
    out = {}
    skipped = 0
    for path in glob.glob(os.path.join(DAILY, "*.csv")):
        sym = os.path.basename(path)[:-4].strip().upper()
        df = _read_symbol(path)
        if df is None or len(df) < min_years * 240:
            skipped += 1
            continue
        # value-based liquidity filter over the evaluation period
        # (value column is missing on long-history files -> estimate volume*close)
        d = df[(df["date"] >= start)].copy()
        if len(d) < 240:
            skipped += 1
            continue
        if "value" in df.columns:
            med_val = d["value"].median()
        elif "volume" in df.columns and "close" in df.columns:
            med_val = (d["volume"] * d["close"]).median()
        else:
            med_val = 0.0
        if med_val < min_avg_value:
            skipped += 1
            continue
        # backward split adjustment
        s = splits[splits["symbol"] == sym]
        if len(s):
            # cumulative factor: multiply price at date t by product of splits after t
            s = s[s["date"] >= df["date"].min()]
            s = s.sort_values("date")
            mult = {}
            acc = 1.0
            for _, row in s[::-1].iterrows():  # newest to oldest
                acc *= (row["split"] if row["split"] > 0 else 1.0)
                mult[row["date"]] = acc
            mult_s = pd.Series(mult)
            def apply_mult(date_series):
                return mult_s.asof(date_series).fillna(1.0).values
            fac = df["date"].map(lambda dt: mult_s[mult_s.index <= dt].iloc[-1]
                                 if (mult_s.index <= dt).any() else 1.0)
            for c in ["open", "high", "low", "close"]:
                df[c] = df[c] * fac.values
        cols = [c for c in ["date", "open", "high", "low", "close", "volume", "value"] if c in df.columns]
        out[sym] = df[cols].set_index("date")
    print(f"[broad] loaded {len(out)} symbols, skipped {skipped}")
    return out


def to_panel_wide(symbols, start="2010-01-01", end="2026-06-30"):
    """Return (close_wide, ret_wide)."""
    # load all
    return symbols
