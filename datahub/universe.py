"""Universe manager — add more stocks without touching a single line of code.

The repository already ships ~3,700 raw NSE daily files under
``data/eod2/daily``.  The *clean* parquet bundle the strategy reads from
(``data/clean/eod2_data``) only holds the 133 names that were promoted.  This
module promotes more of them, applying the same backward split/bonus adjustment
used by :mod:`research_live.broad_data`, and writes the result to
``var/cache/broad_universe.parquet`` (gitignored — it is derived data, rebuilt
on demand from files already in the repo).

Why a cache and not more committed parquets: the raw mirror is the source of
truth and is already in git; duplicating it as parquets would add hundreds of
megabytes to every clone for zero new information.

Usage
-----
    python scripts/expand_universe.py --limit 400
    python scripts/expand_universe.py --min-years 8 --min-value 5000000
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from datahub.panel import BROAD_CACHE, BUNDLE_DIR, RAW_EOD_DIR, clear_cache

logger = logging.getLogger(__name__)

OHLCV = ("open", "high", "low", "close", "volume")
DEFAULT_MIN_YEARS = 8
DEFAULT_MIN_VALUE = 10_000_000  # ₹1 crore median daily traded value (research card rule)
DEFAULT_START = "2010-01-01"

_build_lock = threading.Lock()
_build_state: dict[str, Any] = {"running": False, "started_at": None, "result": None}


def _corporate_actions_path() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "corporate_actions.csv"


def load_splits() -> pd.DataFrame:
    """Split multipliers by (date, symbol) from ``data/corporate_actions.csv``."""
    path = _corporate_actions_path()
    if not path.is_file():
        return pd.DataFrame(columns=["date", "symbol", "split"])
    frame = pd.read_csv(path)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    return frame[["date", "symbol", "split"]].dropna(subset=["date", "symbol"])


def _read_raw(path: Path) -> pd.DataFrame | None:
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError):
        return None
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    if "date" not in frame.columns or "close" not in frame.columns:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    if "series" in frame.columns:
        frame = frame[frame["series"].astype(str).str.upper() == "EQ"]
    frame = frame.sort_values("date").drop_duplicates("date")
    if frame.empty:
        return None
    for column in OHLCV:
        if column not in frame.columns:
            frame[column] = np.nan
    return frame


def _split_adjust(frame: pd.DataFrame, symbol: str, splits: pd.DataFrame) -> pd.DataFrame:
    """Backward split/bonus adjustment (identical convention to research_live)."""
    if splits.empty:
        return frame
    rows = splits[splits["symbol"] == symbol].sort_values("date")
    rows = rows[rows["date"] >= frame["date"].min()]
    if rows.empty:
        return frame
    factor = pd.Series(
        {
            row.date: float(row.split) if float(row.split) > 0 else 1.0
            for row in rows.itertuples()
        }
    ).sort_index()
    cumulative = factor[::-1].cumprod()[::-1]
    applied = frame["date"].map(
        lambda stamp: float(cumulative[cumulative.index <= stamp].iloc[-1])
        if (cumulative.index <= stamp).any()
        else 1.0
    )
    out = frame.copy()
    for column in ("open", "high", "low", "close"):
        out[column] = out[column] * applied.to_numpy()
    return out


def bundle_symbols() -> set[str]:
    return {p.stem.upper() for p in BUNDLE_DIR.glob("*.parquet")}


def scan_candidates(*, min_bars: int = 250) -> list[dict[str, Any]]:
    """Cheap inventory of the raw mirror: what could be promoted, and when it ends."""
    promoted = bundle_symbols()
    rows: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(RAW_EOD_DIR / "*.csv"))):
        symbol = Path(path).stem.strip().upper()
        if symbol in promoted:
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        rows.append(
            {
                "symbol": symbol,
                "path": path,
                "bytes": size,
                "promoted": False,
                "est_bars": int(size / 60),
            }
        )
    rows = [r for r in rows if r["est_bars"] >= min_bars]
    rows.sort(key=lambda r: -r["bytes"])
    return rows


def build_broad(
    *,
    min_years: float = DEFAULT_MIN_YEARS,
    min_avg_value: float = DEFAULT_MIN_VALUE,
    start: str = DEFAULT_START,
    limit: int | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    """Promote liquid raw symbols into ``var/cache/broad_universe.parquet``.

    Filters (identical in spirit to ``research_live.broad_data``):

    * at least ``min_years`` of history (~240 trading days a year),
    * median daily traded value ≥ ``min_avg_value`` over the evaluation window,
    * ``series == EQ`` rows only,
    * backward split/bonus adjustment from ``data/corporate_actions.csv``.
    """
    if not _build_lock.acquire(blocking=False):
        return {"running": True, "error": "a build is already in progress"}
    try:
        _build_state.update({"running": True, "started_at": time.time(), "result": None})
        started = time.time()
        splits = load_splits()
        promoted = bundle_symbols()
        wanted = {s.strip().upper() for s in symbols} if symbols else None
        frames: list[pd.DataFrame] = []
        accepted: list[str] = []
        skipped = {"too_short": 0, "illiquid": 0, "unreadable": 0, "not_requested": 0}
        paths = sorted(glob.glob(str(RAW_EOD_DIR / "*.csv")))
        for path in paths:
            symbol = Path(path).stem.strip().upper()
            if symbol in promoted:
                continue
            if wanted is not None and symbol not in wanted:
                skipped["not_requested"] += 1
                continue
            frame = _read_raw(Path(path))
            if frame is None or len(frame) < min_years * 240:
                skipped["too_short"] += 1
                continue
            window = frame[frame["date"] >= pd.Timestamp(start)]
            if len(window) < 240:
                skipped["too_short"] += 1
                continue
            if "value" in frame.columns and frame["value"].notna().any():
                median_value = float(window["value"].median())
            else:
                median_value = float((window["volume"] * window["close"]).median())
            if median_value < min_avg_value:
                skipped["illiquid"] += 1
                continue
            frame = _split_adjust(frame, symbol, splits)
            out = frame[["date", *OHLCV]].copy()
            out["symbol"] = symbol
            out = out[["date", "symbol", *OHLCV]]
            out = out[out["close"] > 0]
            frames.append(out)
            accepted.append(symbol)
            if limit is not None and len(accepted) >= limit:
                break

        result: dict[str, Any] = {
            "accepted": len(accepted),
            "skipped": skipped,
            "seconds": round(time.time() - started, 1),
            "min_years": min_years,
            "min_avg_value": min_avg_value,
            "start": start,
            "limit": limit,
            "cache": str(BROAD_CACHE),
        }
        if not frames:
            result["error"] = (
                "no raw symbol passed the filters — lower --min-years or --min-value"
            )
            _build_state.update({"running": False, "result": result})
            return result

        panel = pd.concat(frames, ignore_index=True)
        panel = panel.drop_duplicates(subset=["date", "symbol"], keep="last")
        panel = panel.sort_values(["date", "symbol"])
        for column in OHLCV:
            panel[column] = panel[column].astype("float64")
        BROAD_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BROAD_CACHE.with_suffix(".parquet.tmp")
        panel.to_parquet(tmp, index=False)
        tmp.replace(BROAD_CACHE)
        clear_cache()
        result["rows"] = int(len(panel))
        result["symbols"] = sorted(accepted)
        result["date_range"] = (
            f"{pd.Timestamp(panel['date'].min()).date()} to "
            f"{pd.Timestamp(panel['date'].max()).date()}"
        )
        result["cache_mb"] = round(BROAD_CACHE.stat().st_size / 1_048_576, 2)
        _build_state.update({"running": False, "result": result})
        return result
    finally:
        _build_lock.release()


def build_async(**kwargs: Any) -> dict[str, Any]:
    """Start :func:`build_broad` in a background thread (used by the dashboard)."""
    thread = threading.Thread(
        target=lambda: build_broad(**kwargs), name="universe-build", daemon=True
    )
    thread.start()
    return {"running": True, "message": "universe build started in the background"}


def status() -> dict[str, Any]:
    """Current universe-expansion state for the Operations / Overview pages."""
    out: dict[str, Any] = {
        "bundle_dir": str(BUNDLE_DIR),
        "bundle_symbols": len(bundle_symbols()),
        "raw_files": len(glob.glob(str(RAW_EOD_DIR / "*.csv"))),
        "cache_exists": BROAD_CACHE.is_file(),
        "cache_path": str(BROAD_CACHE),
        "build": dict(_build_state),
    }
    if BROAD_CACHE.is_file():
        try:
            import pyarrow.parquet as pq

            meta = pq.read_metadata(BROAD_CACHE)
            out["cache_symbols"] = None
            out["cache_rows"] = int(meta.num_rows)
            out["cache_mb"] = round(BROAD_CACHE.stat().st_size / 1_048_576, 2)
            table = pd.read_parquet(BROAD_CACHE, columns=["symbol", "date"])
            out["cache_symbols"] = int(table["symbol"].nunique())
            out["cache_last_bar"] = str(pd.Timestamp(table["date"].max()).date())
        except Exception as exc:  # noqa: BLE001 - status must never raise
            out["cache_error"] = str(exc)
    return out
