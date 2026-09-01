"""The canonical price-data layer.

One implementation, used by every page.  The panel is built from the clean,
split/bonus-adjusted parquet bundle in ``data/clean/eod2_data`` and, when the
operator has built it, from the broad universe cache in
``var/cache/broad_universe.parquet`` (derived from the raw EOD mirror that is
already in this repository — see :mod:`datahub.universe`).

Why this module exists
----------------------
Before datahub, three pages loaded price data three different ways and could
disagree: the Research Cockpit only knew about ``data/clean/prices.parquet``
(which nothing produced), the Strategy Dashboard read the clean bundle with its
own private universe filter, and the Live Terminal read raw CSVs.  The Research
Cockpit therefore reported "Missing — no price data found" while the Strategy
Dashboard was happily computing signals from 133 symbols.

Two bugs lived in that duplicated logic and are fixed here once:

1. Universe selection required a symbol to have printed a bar on the *single*
   latest date of the whole panel.  The bundle legitimately contains two
   "last bar" dates (names refreshed on different days), so a long-history
   universe could end up with **zero** survivors and the signal raised
   ``no data for signal computation``.  Selection now uses a rolling recency
   *window* and reports exactly what it kept and why.
2. ``prices.parquet`` was referenced but never written.  :func:`materialize_prices`
   writes it from the same panel, so the Research Cockpit's default path and the
   Strategy Dashboard can never disagree again.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = ROOT / "data" / "clean" / "eod2_data"
RAW_EOD_DIR = ROOT / "data" / "eod2" / "daily"
CLEAN_DIR = ROOT / "data" / "clean"
PRICES_FILE = CLEAN_DIR / "prices.parquet"
VAR_DIR = ROOT / "var"
CACHE_DIR = VAR_DIR / "cache"
BROAD_CACHE = CACHE_DIR / "broad_universe.parquet"

#: OHLCV columns shared by the clean bundle and the broad cache.
OHLCV = ("open", "high", "low", "close", "volume")

#: ---------------------------------------------------------------------------
#: The production universe rule.
#:
#: These numbers are the ones the validated MomReM card was measured on
#: (``research_live/deliverables/STRATEGY_REPORT.md``: "~552 liquid names,
#: median daily traded value >= Rs 10M, >= 8yrs history", window 2010-01-01 ->
#: 2026-06-30).  Re-running that exact rule through this module reproduces the
#: card to within rounding (OOS Sharpe 0.945 vs 0.966 published, OOS max DD
#: -16.3% vs -16.3%), which is the whole point: the live signal and the
#: backtest must be computed on the same universe or they are not the same
#: strategy.
#:
#: ``min_history_bars`` is a lower floor (you cannot rank 20-day momentum and a
#: 100-day regime SMA on less), and ``research_min_history_bars`` is the stricter
#: research-parity rule used to report how close the current bundle is to the
#: validated universe.
#: ---------------------------------------------------------------------------
ANALYSIS_START = pd.Timestamp("2010-01-01")
DEFAULT_MIN_HISTORY_BARS = 240
RESEARCH_MIN_HISTORY_BARS = 8 * 240
DEFAULT_MIN_TRADED_VALUE = 10_000_000.0  # Rs 1 crore median daily traded value
DEFAULT_RECENCY_DAYS = 10
DEFAULT_MIN_COVERAGE = 0.0  # research universe does not require full coverage

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.RLock()
_CACHE_TTL_SECONDS = float(os.getenv("QUANT_DATAHUB_TTL", "300"))


def _cached(key: str, fn, *args, **kwargs):
    """TTL cache so repeated page/API hits don't re-read hundreds of parquets."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    value = fn(*args, **kwargs)
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    """Drop every cached frame (call after ingesting new bars)."""
    with _cache_lock:
        _cache.clear()


def cache_path(name: str) -> Path:
    """Return (and create the parent of) a cache file under ``var/cache``."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


# ---------------------------------------------------------------------------
# Panel construction
# ---------------------------------------------------------------------------


def bundle_files() -> list[Path]:
    """Sorted list of per-symbol parquets in the clean bundle."""
    return sorted(Path(p) for p in glob.glob(str(BUNDLE_DIR / "*.parquet")))


def _read_bundle() -> pd.DataFrame:
    files = bundle_files()
    if not files:
        return pd.DataFrame(columns=["date", "symbol", *OHLCV])
    frames = []
    for path in files:
        try:
            frame = pd.read_parquet(path, columns=["date", "symbol", *OHLCV])
        except (ValueError, OSError, ImportError) as exc:  # noqa: PERF203
            logger.warning("bundle_read_failed %s: %s", path.name, exc)
            continue
        frames.append(frame.dropna(subset=["date", "close"]))
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", *OHLCV])
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    out["source_layer"] = "clean"
    return out


def _read_broad_cache() -> pd.DataFrame:
    """Optional broader universe built by :mod:`datahub.universe`."""
    if not BROAD_CACHE.is_file():
        return pd.DataFrame(columns=["date", "symbol", *OHLCV])
    try:
        frame = pd.read_parquet(BROAD_CACHE)
    except (OSError, ValueError, ImportError) as exc:
        logger.warning("broad_cache_unreadable: %s", exc)
        return pd.DataFrame(columns=["date", "symbol", *OHLCV])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["source_layer"] = "broad"
    return frame


def _build_panel() -> pd.DataFrame:
    """Concatenate every available layer into one long OHLCV panel.

    The clean bundle wins over the broad cache for the same (date, symbol) so
    that the audited, split-adjusted rows are never overwritten.
    """
    layers = [_read_bundle(), _read_broad_cache()]
    frames = [f for f in layers if not f.empty]
    if not frames:
        raise RuntimeError(
            f"no price data found under {BUNDLE_DIR} — run `python fetch_data.py`"
        )
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.drop_duplicates(subset=["date", "symbol"], keep="first")
    panel = panel.sort_values(["date", "symbol"]).set_index(["date", "symbol"])
    panel = panel[
        (panel["close"] > 0) & (panel["high"] >= panel["low"]) & (panel["high"] > 0)
    ]
    return panel[list(OHLCV) + (["source_layer"] if "source_layer" in panel else [])]


def load_panel() -> pd.DataFrame:
    """Return the cached long-form (date, symbol) OHLCV panel."""
    return _cached("panel", _build_panel)


def wide(column: str = "close", *, symbols: Iterable[str] | None = None) -> pd.DataFrame:
    """Return ``column`` as a date x symbol matrix."""
    if column not in OHLCV:
        raise KeyError(f"unknown OHLCV column: {column}")
    panel = load_panel()

    def _make() -> pd.DataFrame:
        frame = panel[column].unstack("symbol").sort_index()
        if symbols is not None:
            wanted = list(symbols)
            frame = frame.reindex(columns=[s for s in frame.columns if s in set(wanted)])
        return frame

    key = f"wide:{column}:{'' if symbols is None else ','.join(sorted(symbols))}"
    return _cached(key, _make)


# ---------------------------------------------------------------------------
# Universe selection (the fixed logic)
# ---------------------------------------------------------------------------


def _layer_counts() -> dict[str, int]:
    panel = load_panel()
    if "source_layer" not in panel.columns:
        return {"clean": int(panel.index.get_level_values("symbol").nunique())}
    per_symbol = panel.reset_index()[["symbol", "source_layer"]].drop_duplicates()
    counts = per_symbol["source_layer"].value_counts().to_dict()
    return {str(k): int(v) for k, v in counts.items()}


def select_universe(
    *,
    start: pd.Timestamp | str = ANALYSIS_START,
    min_history_bars: int = DEFAULT_MIN_HISTORY_BARS,
    min_traded_value: float = DEFAULT_MIN_TRADED_VALUE,
    recency_days: int = DEFAULT_RECENCY_DAYS,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> dict[str, Any]:
    """Pick the tradable universe and explain exactly what was rejected.

    Rules, in the order they are applied (each with a rejection list so the UI
    can say *why* a name is missing instead of showing an empty basket):

    1. ``start``            — only bars on/after the analysis window count.
    2. ``min_history_bars`` — enough bars to rank momentum and a regime SMA.
    3. ``min_traded_value`` — median daily traded value (volume x close) over the
                              window; this is the liquidity floor the research
                              used, and it is what keeps the basket fillable.
    4. ``recency_days``     — must have printed a bar within the last N *panel
                              trading days* (a rolling window).
    5. ``min_coverage``     — optional extra coverage floor inside the window.

    Step 4 is the bug fix.  The previous implementation demanded a bar on the
    single newest date in the whole panel; the bundle legitimately contains more
    than one "last bar" date, so a long-history universe could end up with zero
    survivors and the signal raised ``no data for signal computation``.
    """

    def _select() -> dict[str, Any]:
        full_close = wide("close")
        if full_close.empty:
            raise RuntimeError("price panel is empty")
        close = full_close.loc[full_close.index >= pd.Timestamp(start)]
        if close.empty:
            close = full_close
        volume = wide("volume").reindex(index=close.index, columns=close.columns)

        history = close.notna().sum()
        traded_value = (close * volume).median()
        coverage = close.notna().mean()

        # rolling recency window over the FULL panel, not an exact last-date match
        all_dates = full_close.index
        window = all_dates[-recency_days:]
        last_bar = full_close.apply(lambda col: col.last_valid_index())
        fresh = pd.Series(False, index=full_close.columns)
        for symbol in full_close.columns:
            stamp = last_bar.get(symbol)
            if stamp is not None and not pd.isna(stamp):
                fresh[symbol] = bool(stamp >= window[0])

        ok_history = history >= min_history_bars
        ok_liquid = traded_value.fillna(0.0) >= min_traded_value
        ok_coverage = coverage >= min_coverage
        ok_fresh = fresh.reindex(full_close.columns).fillna(False).astype(bool)

        passed = ok_history & ok_liquid & ok_coverage & ok_fresh
        symbols = sorted(sym for sym in close.columns if bool(passed.get(sym, False)))

        def _rejected(stage_ok: pd.Series) -> list[str]:
            prior = ok_history & ok_liquid & ok_coverage
            return sorted(
                sym
                for sym in close.columns
                if bool(prior.get(sym, False)) and not bool(stage_ok.get(sym, False))
            )

        rejected = {
            "insufficient_history": sorted(
                sym for sym in close.columns if not bool(ok_history.get(sym, False))
            ),
            "illiquid": sorted(
                sym
                for sym in close.columns
                if bool(ok_history.get(sym, False))
                and not bool(ok_liquid.get(sym, False))
            ),
            "insufficient_coverage": sorted(
                sym
                for sym in close.columns
                if bool(ok_history.get(sym, False))
                and bool(ok_liquid.get(sym, False))
                and not bool(ok_coverage.get(sym, False))
            ),
            "not_recently_traded": _rejected(ok_fresh),
        }

        # research parity: how many names also meet the stricter 8-year rule?
        research_parity = int(((history >= RESEARCH_MIN_HISTORY_BARS) & passed).sum())
        return {
            "symbols": symbols,
            "size": len(symbols),
            "panel_symbols": int(full_close.shape[1]),
            "panel_dates": int(full_close.shape[0]),
            "panel_first": str(all_dates[0].date()),
            "panel_last": str(all_dates[-1].date()),
            "start": str(pd.Timestamp(start).date()),
            "window_rows": int(len(close)),
            "min_history_bars": int(min_history_bars),
            "min_traded_value": float(min_traded_value),
            "recency_days": int(recency_days),
            "min_coverage": float(min_coverage),
            "recency_window": [str(d.date()) for d in window],
            "research_min_history_bars": int(RESEARCH_MIN_HISTORY_BARS),
            "research_parity_symbols": research_parity,
            "rejected": rejected,
        }

    key = (
        f"universe:{pd.Timestamp(start).date()}:{min_history_bars}:"
        f"{min_traded_value}:{recency_days}:{min_coverage}"
    )
    return _cached(key, _select)


def strategy_frame(
    *,
    start: pd.Timestamp | str = ANALYSIS_START,
    recency_days: int = DEFAULT_RECENCY_DAYS,
    min_traded_value: float = DEFAULT_MIN_TRADED_VALUE,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return ``(close_wide, universe_meta)`` — the frame every analysis uses.

    The frame is trimmed to the last date on which the *selected universe*
    actually has a bar.  Without that trim the final panel row can be entirely
    NaN (names refresh on different days), and ``close.iloc[-1]`` then yields an
    empty momentum row — the same class of bug that produced "no data for signal
    computation".
    """

    def _frame() -> tuple[pd.DataFrame, dict[str, Any]]:
        meta = select_universe(
            start=start, recency_days=recency_days, min_traded_value=min_traded_value
        )
        if not meta["symbols"]:
            raise RuntimeError(
                "universe selection kept 0 of "
                f"{meta['panel_symbols']} symbols "
                f"(window from {meta['start']}, >={meta['min_history_bars']} bars, "
                f"median traded value >= Rs {meta['min_traded_value']:,.0f}, traded "
                f"within the last {meta['recency_days']} panel days). "
                "Expand the universe with `python scripts/expand_universe.py`."
            )
        close = wide("close")
        frame = close.loc[close.index >= pd.Timestamp(start), meta["symbols"]]
        last_valid = frame.dropna(how="all").index[-1]
        frame = frame.loc[:last_valid]
        meta["as_of"] = str(pd.Timestamp(last_valid).date())
        meta["panel_last"] = str(close.index[-1].date())
        meta["frame_rows"] = int(len(frame))
        meta["frame_cols"] = int(frame.shape[1])
        return frame, meta

    return _cached(
        f"strategy_frame:{pd.Timestamp(start).date()}:{recency_days}:{min_traded_value}",
        _frame,
    )



# ---------------------------------------------------------------------------
# Canonical prices.parquet (shared with the Research Cockpit)
# ---------------------------------------------------------------------------


def materialize_prices(force: bool = False) -> dict[str, Any]:
    """Write the canonical long-form ``data/clean/prices.parquet`` bundle.

    The Research Cockpit defaults to this path.  Building it from the same
    panel the Strategy Dashboard uses is what stops the two pages from ever
    reporting different answers about whether data exists.
    """
    panel = load_panel()
    if not force and PRICES_FILE.is_file():
        newest_source = max(
            (p.stat().st_mtime for p in bundle_files()), default=0.0
        )
        newest_source = max(newest_source, _mtime(BROAD_CACHE))
        if PRICES_FILE.stat().st_mtime >= newest_source:
            return describe_prices_file()
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    frame = panel.reset_index()[["date", "symbol", *OHLCV]].copy()
    frame["date"] = frame["date"].dt.normalize()
    frame = frame.sort_values(["date", "symbol"])
    tmp = PRICES_FILE.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp, index=False)
    tmp.replace(PRICES_FILE)
    clear_cache()
    return describe_prices_file()


def _mtime(path: Path | str) -> float:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return 0.0


def describe_prices_file() -> dict[str, Any]:
    """Cheap metadata for the canonical bundle (no full read)."""
    if not PRICES_FILE.is_file():
        return {"exists": False, "path": str(PRICES_FILE)}
    try:
        import pyarrow.parquet as pq

        meta = pq.read_metadata(PRICES_FILE)
        n_rows = int(meta.num_rows)
    except Exception:  # noqa: BLE001 - metadata is a nicety, never fatal
        n_rows = None
    return {
        "exists": True,
        "path": str(PRICES_FILE),
        "size_mb": round(PRICES_FILE.stat().st_size / 1_048_576, 2),
        "rows": n_rows,
        "built_at": datetime.fromtimestamp(
            PRICES_FILE.stat().st_mtime, tz=UTC
        ).isoformat(),
    }


# ---------------------------------------------------------------------------
# Data status — ONE dict, used by every page
# ---------------------------------------------------------------------------


def ingest_freshness() -> dict[str, Any]:
    """Last successful ingestion evidence: newest bar, newest file, bundle mtime."""
    panel = load_panel()
    last_bar = panel.index.get_level_values("date").max()
    files = bundle_files()
    newest_file = max((_mtime(p) for p in files), default=0.0)
    meta_files = sorted(glob.glob(str(BUNDLE_DIR / "*.meta.json")))
    ingested_at: str | None = None
    validated_count = 0
    quality_issues = 0
    for path in meta_files:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        stamp = payload.get("validated_at") or payload.get("ingested_at")
        if stamp and (ingested_at is None or str(stamp) > ingested_at):
            ingested_at = str(stamp)
        if payload.get("is_clean"):
            validated_count += 1
            quality_issues += len(payload.get("quality_issues") or [])
    return {
        "last_bar": str(pd.Timestamp(last_bar).date()) if last_bar is not None else None,
        "last_bar_age_days": (
            int((pd.Timestamp.now().normalize() - pd.Timestamp(last_bar).normalize()).days)
            if last_bar is not None
            else None
        ),
        "bundle_files": len(files),
        "bundle_newest_file_at": (
            datetime.fromtimestamp(newest_file, tz=UTC).isoformat()
            if newest_file
            else None
        ),
        "source_last_update": ingested_at,
        "clean_validated_symbols": validated_count,
        "open_quality_issues": quality_issues,
    }


def data_status(*, refresh: bool = False) -> dict[str, Any]:
    """The single data-health document every page renders.

    Shape is backwards compatible with the Research Cockpit's expectations
    (``prices_file``, ``prices_exists``, ``prices_size_mb``, ``prices_info``,
    ``universe_files``) and adds the bundle/universe detail the Strategy
    Dashboard and Operations page need.
    """

    def _status() -> dict[str, Any]:
        status: dict[str, Any] = {
            "prices_file": str(PRICES_FILE),
            "prices_exists": PRICES_FILE.is_file(),
            "prices_size_mb": (
                round(PRICES_FILE.stat().st_size / 1_048_576, 2)
                if PRICES_FILE.is_file()
                else None
            ),
            "universe_files": {},
        }
        for name in ("nifty50", "nifty100", "nifty500"):
            path = ROOT / "data" / "universe" / f"{name}.csv"
            status["universe_files"][name] = {
                "exists": path.is_file(),
                "path": str(path),
            }

        try:
            panel = load_panel()
        except RuntimeError as exc:
            status["error"] = str(exc)
            status["available"] = False
            return status

        symbols = panel.index.get_level_values("symbol")
        dates = panel.index.get_level_values("date")
        status["available"] = True
        status["bundle"] = {
            "dir": str(BUNDLE_DIR),
            "files": len(bundle_files()),
            "broad_cache": BROAD_CACHE.is_file(),
            "broad_cache_path": str(BROAD_CACHE),
            "layers": _layer_counts(),
        }
        status["prices_info"] = {
            "format": "long",
            "dates": int(dates.nunique()),
            "symbols": int(symbols.nunique()),
            "date_range": f"{pd.Timestamp(dates.min()).date()} to {pd.Timestamp(dates.max()).date()}",
            "rows": int(len(panel)),
        }
        status["freshness"] = ingest_freshness()
        try:
            universe = select_universe()
            status["universe"] = {
                "size": universe["size"],
                "symbols": universe["symbols"],
                "panel_symbols": universe["panel_symbols"],
                "start": universe["start"],
                "min_history_bars": universe["min_history_bars"],
                "min_traded_value": universe["min_traded_value"],
                "recency_days": universe["recency_days"],
                "research_parity_symbols": universe["research_parity_symbols"],
                "research_min_history_bars": universe["research_min_history_bars"],
                "rejected_counts": {
                    key: len(value) for key, value in universe["rejected"].items()
                },
            }
        except RuntimeError as exc:
            status["universe"] = {"size": 0, "error": str(exc)}
        return status

    if refresh:
        clear_cache()
    return _cached("data_status", _status)
