"""Load and prepare the local NSE EOD2 dataset for discovery.

Data contract
-------------
Raw data lives at ``data/raw/eod2_data/NSE/{symbol}/{year}/{month}.parquet``.
Each file is a long frame with date, symbol, open/high/low/close, volume,
series, source, exchange, adjustment_state. All prices are split/bonus
adjusted. We pivot the monthly files into daily wide panels and cache them in
``data/features/`` (git-ignored) for fast iteration.

Universe
--------
We use the point-in-time Nifty 100 membership CSV
(``data/universe/nifty100-pit/nifty100.csv``) for survivorship-aware membership
masks. Only symbols that actually have price data locally can be traded in a
backtest; the price history itself is still biased toward symbols that are
currently listed / had data fetched, which we report openly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW_GLOB = "data/raw/eod2_data/NSE/*/*/*.parquet"
FEATURES_DIR = ROOT / "data" / "features"
PIT_UNIVERSE_CSV = ROOT / "data" / "universe" / "nifty100-pit" / "nifty100.csv"
PANEL_SYMBOLS_TXT = ROOT / "data" / "universe" / "nifty100-pit" / "panel_symbols.txt"


def _cache(name: str) -> Path:
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)
    return FEATURES_DIR / name


def load_eod_long() -> pd.DataFrame:
    """Load the full long OHLCV frame from the raw monthly parquet files."""
    # DuckDB is ~10x faster than pandas for the 5.6k monthly files.
    con = duckdb.connect()
    columns = ", ".join(["date", "symbol", "open", "high", "low", "close", "volume"])
    sql = f"""
        SELECT {columns}
        FROM read_parquet('{RAW_GLOB}', union_by_name=true)
        ORDER BY date, symbol
    """
    return con.execute(sql).df()


def load_eod_panels(force: bool = False) -> dict[str, pd.DataFrame]:
    """Return wide daily panels for open/high/low/close/volume.

    Symbols are columns; dates are a sorted DatetimeIndex. The panels are
    cached under ``data/features`` so repeated experiments do not re-read all
    monthly files.
    """
    out: dict[str, pd.DataFrame] = {}
    for field in ("open", "high", "low", "close", "volume"):
        path = _cache(f"eod2_panel_{field}.parquet")
        if not force and path.exists():
            out[field] = pd.read_parquet(path)
            continue
        long = load_eod_long()
        panel = long.pivot(index="date", columns="symbol", values=field).sort_index()
        panel.index = pd.to_datetime(panel.index)
        # The raw parquet contains a small number of Saturday/Sunday bars.
        # NSE is closed on weekends, so those rows are not tradable bars and
        # must be removed before backtesting.
        panel = panel.loc[panel.index.dayofweek < 5]
        out[field] = panel
        panel.to_parquet(path)
    return out


def load_pit_universe() -> pd.DataFrame:
    """Load point-in-time Nifty 100 membership as a long frame."""
    frame = pd.read_csv(PIT_UNIVERSE_CSV)
    frame["valid_from"] = pd.to_datetime(frame["valid_from"])
    frame["valid_to"] = pd.to_datetime(frame["valid_to"])
    frame["symbol"] = frame["symbol"].str.upper()
    return frame


def panel_universe_mask(
    universe: pd.DataFrame, dates: pd.DatetimeIndex, symbols: Iterable[str]
) -> pd.DataFrame:
    """Return a date x symbol boolean mask of index membership."""
    symbols = list(symbols)
    index = pd.DatetimeIndex(dates)
    mask = pd.DataFrame(False, index=index, columns=symbols)
    for symbol in symbols:
        rows = universe.loc[universe["symbol"] == symbol]
        for _, row in rows.iterrows():
            start = row["valid_from"]
            end = row["valid_to"]
            sub = (index >= start) & ((pd.isna(end)) | (index <= end))
            mask.loc[sub, symbol] = True
    return mask


def clean_panel_symbols(
    close: pd.DataFrame, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None
) -> list[str]:
    """Return symbols whose close series is complete over the requested window."""
    start = pd.Timestamp(start) if start is not None else close.index.min()
    end = pd.Timestamp(end) if end is not None else close.index.max()
    chained = start <= close.index <= end
    # Tolerance: allow a small number of missing days (holiday gaps are fine), but
    # no missing leading/trailing window.
    sub = close.loc[chained].copy()
    return [
        sym
        for sym in sub.columns
        if sub[sym].first_valid_index() is not None
        and sub[sym].last_valid_index() is not None
        and sub[sym].first_valid_index() <= start
        and sub[sym].last_valid_index() >= end
    ]


def panel_symbols_from_file() -> list[str]:
    """Read the documented clean research panel symbol list."""
    lines = PANEL_SYMBOLS_TXT.read_text().splitlines()
    return [
        line.strip().upper()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def resolve_research_universe(
    close: pd.DataFrame, universe: pd.DataFrame | None = None
) -> list[str]:
    """Return the point-in-time research universe actually usable locally.

    This is the set of PIT Nifty 100 members that had any price history during
    the available window *and* have local price data. It excludes names with no
    local data (e.g. HDFC, a delisted member merged into HDFCBANK in 2023),
    which is reported openly as survivorship data gap.

    Unlike ``panel_symbols_from_file``, this includes recent listings / spins
    (JIOFIN, SWIGGY, HYUNDAI, TATACAP, TMCV, BAJAJHFL, ENRIN) so that a backtest
    can let them enter the investable set at their actual PIT membership date.
    """
    universe = universe if universe is not None else load_pit_universe()
    universe = universe.copy()
    universe["valid_from"] = pd.to_datetime(universe["valid_from"])
    universe["valid_to"] = pd.to_datetime(universe["valid_to"])
    active = universe[
        (universe["valid_from"] <= close.index.max())
        & ((universe["valid_to"].isna()) | (universe["valid_to"] >= close.index.min()))
    ]
    return sorted(set(active["symbol"]) & set(close.columns))
