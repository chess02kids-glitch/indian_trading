"""Thin adapter for the ``eod2_data`` NSE end-of-day mirror (v0.7 real-data).

``eod2_data`` (github.com/BennyThadikaran/eod2_data) republishes NSE's
official daily equity reports (www.nseindia.com/all-reports) as per-symbol
CSV files. The ``daily/`` series is **adjusted for bonus and splits only**
(dividends are NOT adjusted) per the upstream README.

Design rules (v0.7 §4/§5):

* The adapter only *normalises* source files into the canonical long-form
  OHLCV research contract (``symbol, exchange, date, open, high, low,
  close, volume, source, ingested_at`` plus provenance/adjustment fields).
  It never re-samples, fills, or re-orders data.
* Raw rows are written to the immutable raw layer by the caller
  (``StorageManager``); validation happens in :mod:`data.quality`.
* ``ingested_at`` carries the *source data timestamp* (``meta.json``
  ``lastUpdate``) instead of the wall clock so that re-ingestion of the
  same pinned source commit is byte-for-byte reproducible (v0.7 §14).
  The wall-clock ingestion time is recorded in the provenance manifest
  instead of the data frames.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from data.quality import (
    DataQualityError,
    DataQualityReport,
    check_ohlcv_long_frame,
)

__all__ = [
    "EOD2_SOURCE",
    "EQ_SERIES",
    "Eod2SourceSpec",
    "Eod2SymbolFile",
    "parse_eod2_daily_file",
    "symbol_to_filename",
    "load_eod2_symbol",
    "normalise_eod2_frames",
]

#: Canonical source label recorded on every normalised row.
EOD2_SOURCE = "eod2_data"

#: Upstream ``daily/*.csv`` header (order-sensitive).
EOD2_DAILY_HEADER = (
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "Volume",
    "Series",
    "TOTAL_TRADES",
    "QTY_PER_TRADE",
    "DLV_QTY",
)

_SYMBOL_RE = re.compile(r"^[A-Z0-9&.\-]+$")

#: The only NSE series the research pipeline models: plain equity.
#: See ``parse_eod2_daily_file`` (AUDIT-012) for the measured impact.
EQ_SERIES = "EQ"


@dataclass(frozen=True, slots=True)
class Eod2SourceSpec:
    """Pinned provenance for one eod2_data snapshot."""

    repo: str = "BennyThadikaran/eod2_data"
    commit: str = ""
    branch: str = "main"
    license_note: str = (
        "no license file in the repository; data is a republished mirror of "
        "NSE's official public daily reports (www.nseindia.com/all-reports). "
        "Research-only usage is documented in docs/real_data.md"
    )
    adjustment_state: str = "split_bonus_adjusted"
    adjustment_note: str = (
        "daily/ series is adjusted for bonus and splits only; dividends are "
        "NOT adjusted (upstream README)"
    )
    data_version: str = ""
    last_update: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_meta(
        cls,
        meta: Mapping[str, Any],
        *,
        commit: str,
    ) -> "Eod2SourceSpec":
        """Build a spec from the source repository's ``meta.json``."""
        return cls(
            commit=commit,
            data_version=str(meta.get("data-version", "")),
            last_update=str(meta.get("lastUpdate", "")),
            extra={
                "equity_actions_window": (str(meta.get("equityActionsExpiry", ""))),
            },
        )

    @property
    def ingested_at(self) -> str:
        """Deterministic provenance timestamp (source data timestamp)."""
        return self.last_update or "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": EOD2_SOURCE,
            "repo": self.repo,
            "branch": self.branch,
            "commit": self.commit,
            "license": self.license_note,
            "adjustment_state": self.adjustment_state,
            "adjustment_note": self.adjustment_note,
            "data_version": self.data_version,
            "last_update": self.last_update,
            "ingested_at": self.ingested_at,
            **dict(self.extra),
        }


@dataclass(frozen=True, slots=True)
class Eod2SymbolFile:
    """One source file's identity + content fingerprint.

    ``rows`` counts rows in the *parsed* frame, i.e. after the series filter
    has been applied. ``dropped_series`` reports what the filter removed.
    """

    symbol: str
    path: Path
    file_sha256: str
    rows: int
    first_date: str
    last_date: str
    dropped_series: Mapping[str, int] = field(default_factory=dict)


def symbol_to_filename(symbol: str) -> str:
    """Map a canonical NSE symbol to its eod2 ``daily/`` file name.

    eod2 lower-cases symbols (``M&M`` -> ``m&m.csv``); nothing else changes.
    """
    normalized = symbol.strip().upper()
    if not normalized or not _SYMBOL_RE.match(normalized):
        raise ValueError(f"invalid NSE symbol {symbol!r}")
    return normalized.lower() + ".csv"


def _read_raw_csv(path: str | Path) -> pd.DataFrame:
    """Read one eod2 CSV, normalising header case and validating the header.

    AUDIT-005: the module docstring and ``EOD2_DAILY_HEADER`` claim an
    order-sensitive strict header check, but none existed. A source file with a
    truncated header made ``pandas`` promote the first data column to the index,
    after which ``parse_eod2_daily_file`` raised ``AttributeError: Can only use
    .dt accessor with datetimelike values`` — an opaque crash instead of the
    documented :class:`DataQualityError`. Validate the header explicitly.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"eod2 source file missing: {path}")
    raw = pd.read_csv(path)
    # Map headers to standard title-case format
    header_map = {
        "date": "Date", "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume", "series": "Series"
    }
    raw.rename(columns=lambda x: header_map.get(str(x).strip().lower(), str(x)), inplace=True)

    present = [str(column).strip() for column in raw.columns]
    normalised = [header_map.get(name.lower(), name) for name in present]
    expected = list(EOD2_DAILY_HEADER)
    # The upstream mirror ships two header dialects (``TOTAL_TRADES/QTY_PER_TRADE/
    # DLV_QTY`` and, on ~100 newer symbols, lowercase equivalents). Only the
    # OHLCV prefix is load-bearing for the research contract, so require that
    # prefix in order and let the trailing columns vary.
    required_prefix = expected[:6]
    if normalised[: len(required_prefix)] != required_prefix:
        raise DataQualityError(
            f"unexpected header in {path.name}: expected at least "
            f"{required_prefix} in order, got {present}"
        )
    return raw


def parse_eod2_daily_file(
    path: Path,
    symbol: str,
    *,
    spec: Eod2SourceSpec,
    series_filter: str | None = EQ_SERIES,
    dropped_series: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Normalise one eod2 ``daily/*.csv`` into the canonical long frame.

    The returned frame keeps every **equity** source row (malformed values
    become ``NaN`` so the quality layer can *report* them, not the adapter);
    symbol/date normalisation (uppercase symbol, parsed dates) and the
    provenance columns (``source``, ``exchange``, ``ingested_at``,
    ``source_ts``, ``adjustment_state``) are applied here.

    AUDIT-012 — ``series_filter``: NSE's bhavcopy carries one row per traded
    *series*, and the source files mix them. Measured over **all 3,694
    committed files** (2026-09-01, after the filter was added):

    ==================  =========  =========================================
    series              rows       meaning
    ==================  =========  =========================================
    ``EQ`` (kept)       6,664,240  normal equity market
    ``BE``               500,424   trade-for-trade / odd-lot session
    ``SM``               308,264   institutional / negotiated block
    ``NAN``              269,627   index, VIX and g-sec files (no equity)
    ``ST``                62,225   SME trade-for-trade
    ``BZ``                34,775   negotiated block
    ==================  =========  =========================================

    So **1,175,315 of 7,839,555 rows (14.99%) were not the equity series**
    and were being stitched into one continuous "close" per symbol, which
    produces an artificial jump at every series transition.

    Separately, **741 of 3,694 files contain no EQ row at all** and are now
    refused (``DataQualityError``) instead of contributing an empty or
    misleading history: 566 ``*_SME`` names, 165 Nifty index / g-sec files,
    India VIX, and 9 delisted/odd names. Before the filter those index files
    were ingested as if they were tradeable equities.

    The filter therefore defaults to ``EQ`` and is applied here, at the
    boundary, rather than downstream. Pass ``series_filter=None`` to keep
    every row (the pre-audit behaviour), and pass a dict as
    ``dropped_series`` to learn what was removed: it is populated with
    ``{series: row_count}``.

    A file with rows but no rows in the requested series raises
    :class:`DataQualityError` rather than returning an empty frame — a symbol
    with no equity history is a data problem, not a silent no-op.
    """
    raw = _read_raw_csv(path)
    normalized_symbol = symbol.strip().upper()
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(raw.get("Date", raw.get("date")), errors="coerce"),
            "symbol": normalized_symbol,
            "open": pd.to_numeric(raw.get("Open", raw.get("open")), errors="coerce"),
            "high": pd.to_numeric(raw.get("High", raw.get("high")), errors="coerce"),
            "low": pd.to_numeric(raw.get("Low", raw.get("low")), errors="coerce"),
            "close": pd.to_numeric(raw.get("Close", raw.get("close")), errors="coerce"),
            "volume": pd.to_numeric(raw.get("Volume", raw.get("volume")), errors="coerce"),
        }
    )
    series_column = raw.get("Series", raw.get("series", "")).astype(str)
    if series_filter:
        wanted = series_filter.strip().upper()
        keep = series_column.str.strip().str.upper() == wanted
        if dropped_series is not None:
            for value, count in (
                series_column.loc[~keep].str.strip().str.upper().value_counts().items()
            ):
                dropped_series[str(value)] = dropped_series.get(str(value), 0) + int(count)
        if not keep.any() and len(frame):
            raise DataQualityError(
                f"{path.name}: no rows in series {wanted!r} "
                f"(series present: {sorted(set(series_column.str.strip().str.upper()))})"
            )
        frame = frame.loc[keep].reset_index(drop=True)
        series_column = series_column.loc[keep].reset_index(drop=True)
    frame["series"] = series_column
    frame["source"] = EOD2_SOURCE
    frame["exchange"] = "NSE"
    frame["ingested_at"] = spec.ingested_at
    frame["source_ts"] = frame["date"].dt.strftime("%Y-%m-%d")
    frame["adjustment_state"] = spec.adjustment_state
    return frame


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_eod2_symbol(
    source_dir: str | Path,
    symbol: str,
    *,
    spec: Eod2SourceSpec,
    as_of: Any = None,
) -> tuple[pd.DataFrame, DataQualityReport, Eod2SymbolFile]:
    """Load + validate one symbol's eod2 file.

    Returns ``(accepted, report, file_stats)`` where ``accepted`` follows
    the :func:`data.quality.check_ohlcv_long_frame` contract (malformed,
    duplicate and post-``as_of`` rows are excluded and reported, never
    silently repaired).
    """
    path = Path(source_dir) / symbol_to_filename(symbol)
    dropped: dict[str, int] = {}
    frame = parse_eod2_daily_file(path, symbol, spec=spec, dropped_series=dropped)
    accepted, report = check_ohlcv_long_frame(
        frame,
        source=EOD2_SOURCE,
        exchange="NSE",
        as_of=as_of,
    )
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    stats = Eod2SymbolFile(
        symbol=symbol.strip().upper(),
        path=path,
        file_sha256=_file_sha256(path),
        rows=len(frame),
        first_date=str(dates.min().date()) if len(dates) else "",
        last_date=str(dates.max().date()) if len(dates) else "",
        dropped_series=dict(sorted(dropped.items())),
    )
    return accepted, report, stats


def normalise_eod2_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    source: str = EOD2_SOURCE,
    exchange: str = "NSE",
    as_of: Any = None,
) -> tuple[pd.DataFrame, DataQualityReport]:
    """Validate a batch of already-parsed eod2 frames (concatenated)."""
    if not frames:
        raise DataQualityError("no eod2 frames supplied")
    combined = pd.concat(list(frames.values()), ignore_index=True)
    return check_ohlcv_long_frame(
        combined, source=source, exchange=exchange, as_of=as_of
    )


def load_meta_json(source_dir: str | Path) -> dict[str, Any]:
    """Read the source repository's ``meta.json`` (provenance + calendar)."""
    path = Path(source_dir) / "meta.json"
    if not path.is_file():
        raise FileNotFoundError(f"eod2 meta.json missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_isin_map(source_dir: str | Path) -> dict[str, str]:
    """Read ``isin_symbol_map.json`` (``sym2isin``) from the source repo."""
    path = Path(source_dir) / "isin_symbol_map.json"
    if not path.is_file():
        raise FileNotFoundError(f"eod2 isin_symbol_map.json missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    mapping = payload.get("sym2isin", payload)
    if not isinstance(mapping, dict):
        raise DataQualityError("eod2 isin_symbol_map.json has no sym2isin map")
    return {str(k).strip().upper(): str(v).strip().upper() for k, v in mapping.items()}


def eod2_symbols_available(source_dir: str | Path) -> set[str]:
    """Return every symbol that has a ``daily/`` file in the checkout."""
    daily = Path(source_dir) / "daily"
    if not daily.is_dir():
        return set()
    return {path.stem.upper() for path in daily.glob("*.csv")}


__all__.append("load_meta_json")
__all__.append("load_isin_map")
__all__.append("eod2_symbols_available")
