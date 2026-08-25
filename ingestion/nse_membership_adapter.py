"""Adapter for point-in-time NSE index membership (v0.7 real-data).

Source: ``aditya-jha/nse-historical-membership``
(github.com/aditya-jha/nse-historical-membership) — open-source
point-in-time membership tables for NSE indices derived from public NSE
index press releases and circulars.

* Code license: MIT.
* Data license: **CC BY 4.0** (see the upstream ``LICENSE-DATA``).
  Attribution required: credit the repository and the underlying source
  "NSE press releases / NSE Exchange circulars (publicly published)".
  No warranty; research-only usage (see docs/real_data.md).

The upstream ``index_history/data/index_membership_history.csv`` has CRLF
line endings and the schema::

    index_id,index_name,symbol,valid_from,valid_to,weightage,source,
    source_url,notes

Rows are *membership windows*: a symbol is a member on day ``D`` when
``valid_from <= D <= valid_to`` (empty ``valid_to`` = still a member as
of the source's coverage date). ``source`` is ``press_release``
(per-row ``source_url`` = NSE index press-release PDF) or
``snapshot_floor``/``snapshot`` (inferred from NSE's published current
constituent lists).

This adapter normalises the source into the repository's
:class:`data.universe.UniverseMembership` contract. It never fabricates
membership: every row keeps the source's own ``valid_from``/``valid_to``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = [
    "NIFTY_100_INDEX_ID",
    "NseMembershipSpec",
    "parse_membership_csv",
    "extract_index_rows",
    "members_on_date",
    "build_pit_universe_frame",
    "membership_fingerprint",
    "write_pit_universe",
]

#: Upstream registry id for the Nifty 100 (index_registry.json).
NIFTY_100_INDEX_ID = 219

_MEMBERSHIP_COLUMNS = (
    "index_id",
    "index_name",
    "symbol",
    "valid_from",
    "valid_to",
    "weightage",
    "source",
    "source_url",
    "notes",
)


@dataclass(frozen=True, slots=True)
class NseMembershipSpec:
    """Pinned provenance for one nse-historical-membership snapshot."""

    repo: str = "aditya-jha/nse-historical-membership"
    commit: str = ""
    data_license: str = "CC BY 4.0"
    attribution: str = (
        "Point-in-time NSE index membership from "
        "github.com/aditya-jha/nse-historical-membership (data: CC BY 4.0); "
        "underlying source: NSE index press releases / NSE exchange "
        "circulars (publicly published)"
    )
    coverage_note: str = ""
    extra: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.extra is None:
            object.__setattr__(self, "extra", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "nse-historical-membership",
            "repo": self.repo,
            "commit": self.commit,
            "data_license": self.data_license,
            "attribution": self.attribution,
            "coverage_note": self.coverage_note,
            **dict(self.extra),
        }


def parse_membership_csv(path: str | Path) -> pd.DataFrame:
    """Read the upstream membership CSV (CRLF-safe) with schema validation."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"membership CSV missing: {file_path}")
    frame = pd.read_csv(file_path)
    frame.columns = [str(c).strip() for c in frame.columns]
    missing = [c for c in _MEMBERSHIP_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"membership CSV {file_path.name} missing columns: {missing}")
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    # CRLF-safe date normalisation; empty valid_to = open-ended membership.
    for column in ("valid_from", "valid_to"):
        parsed = pd.to_datetime(frame[column], errors="coerce")
        if column == "valid_from" and parsed.isna().any():
            raise ValueError("membership rows without valid_from are invalid")
        frame[column] = parsed.where(parsed.notna(), None)
    return frame


def extract_index_rows(
    frame: pd.DataFrame,
    *,
    index_id: int = NIFTY_100_INDEX_ID,
    index_name: str | None = None,
) -> pd.DataFrame:
    """Return the rows for one index (by id, optionally verified by name)."""
    rows = frame[frame["index_id"] == index_id]
    if index_name is not None:
        expected = str(index_name).strip().lower()
        names = {str(n).strip().lower() for n in rows["index_name"].unique()}
        if expected not in names:
            raise ValueError(
                f"index {index_id} rows are named {sorted(names)}, not {index_name!r}"
            )
    return rows.sort_values(["symbol", "valid_from"]).reset_index(drop=True)


def members_on_date(
    rows: pd.DataFrame,
    day: str | pd.Timestamp,
) -> set[str]:
    """Resolve the exact members on ``day`` from PIT rows (no gaps)."""
    target = pd.Timestamp(day)
    valid_from = pd.to_datetime(rows["valid_from"], errors="coerce")
    valid_to = pd.to_datetime(rows["valid_to"], errors="coerce")
    mask = valid_from <= target
    open_ended = valid_to.isna()
    closed_after = (valid_to >= target) | open_ended
    return set(rows.loc[mask & closed_after, "symbol"].astype(str))


def build_pit_universe_frame(
    rows: pd.DataFrame,
    *,
    index_name: str = "nifty100",
    isin_map: Mapping[str, str] | None = None,
    exclude_patterns: Sequence[str] = ("DUMMY",),
) -> pd.DataFrame:
    """Convert PIT membership rows into the repository universe schema.

    Output columns match :class:`data.universe.UniverseMembership`:
    ``symbol, index_name, valid_from, valid_to, isin, sector, exchange,
    delisted``.

    * ``delisted`` is set from upstream ``notes`` containing a closure
      marker (``closed by ...``) — i.e. the symbol left the index or the
      exchange; the row is retained (survivorship-bias protection).
    * Symbols matching ``exclude_patterns`` (e.g. NSE demerger dummy
      share rows such as ``DUMMYVEDL1``) are not tradeable equities and
      are dropped *with the drop reported by the caller*, never
      silently.
    """
    kept = rows.copy()
    excluded_symbols = sorted(
        {
            str(s).strip().upper()
            for pattern in exclude_patterns
            for s in kept["symbol"]
            if pattern.lower() in str(s).strip().lower()
        }
    )
    mask = pd.Series(True, index=kept.index)
    for pattern in exclude_patterns:
        mask &= ~kept["symbol"].str.lower().str.contains(pattern.lower())
    kept = kept.loc[mask]
    if kept.empty:
        raise ValueError("no membership rows remain after filtering")

    isin_map = dict(isin_map or {})
    records = []
    for row in kept.to_dict("records"):
        symbol = str(row["symbol"]).strip().upper()
        notes = str(row.get("notes") or "")
        records.append(
            {
                "symbol": symbol,
                "index_name": index_name,
                "valid_from": pd.Timestamp(row["valid_from"]).date().isoformat(),
                "valid_to": (
                    pd.Timestamp(row["valid_to"]).date().isoformat()
                    if row["valid_to"] is not None and pd.notna(row["valid_to"])
                    else None
                ),
                "isin": isin_map.get(symbol),
                "sector": None,
                "exchange": "NSE",
                "delisted": "closed by" in notes.lower(),
                "_source": str(row.get("source") or ""),
                "_source_url": str(row.get("source_url") or ""),
                "_notes": notes,
            }
        )
    frame = pd.DataFrame(records)
    frame.attrs["excluded_symbols"] = excluded_symbols
    frame.attrs["source_row_count"] = int(len(rows))
    frame.attrs["kept_row_count"] = int(len(frame))
    return frame


def membership_fingerprint(rows: pd.DataFrame) -> str:
    """Stable SHA-256 fingerprint of PIT membership rows (sorted)."""
    payload = rows.copy()
    payload["symbol"] = payload["symbol"].astype(str)
    for column in ("valid_from", "valid_to"):
        payload[column] = pd.to_datetime(payload[column], errors="coerce")
        payload[column] = payload[column].dt.strftime("%Y-%m-%d").fillna("")
    payload = payload.sort_values(["symbol", "valid_from", "valid_to"])
    blob = (
        payload[["symbol", "valid_from", "valid_to"]]
        .to_csv(index=False)
        .encode("utf-8")
    )
    return hashlib.sha256(blob).hexdigest()


def _frame_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_pit_universe(
    directory: str | Path,
    frame: pd.DataFrame,
    *,
    spec: NseMembershipSpec,
    source_csv_path: str | Path,
    retrieved_at: str,
) -> Path:
    """Persist the PIT universe (CSV + provenance.json) into ``directory``.

    The CSV columns match the existing ``data/universe/*.csv`` contract so
    :func:`data.universe.load_universe_dataset` can load it unchanged; the
    sidecar ``provenance.json`` records source, license, fingerprint and
    row provenance (per-row source press-release URLs are preserved in
    ``row_provenance``).
    """
    out_dir = Path(directory)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "nifty100.csv"
    public = frame.drop(columns=[c for c in frame.columns if c.startswith("_")])
    public.to_csv(csv_path, index=False)

    kept_symbols = sorted(frame["symbol"].unique())
    provenance = {
        "universe": "nifty100-pit",
        "index_id": NIFTY_100_INDEX_ID,
        "index": "Nifty 100",
        "source": spec.to_dict(),
        "retrieved_at": retrieved_at,
        "source_csv_sha256": _frame_sha256(Path(source_csv_path)),
        "membership_fingerprint": membership_fingerprint(pd.read_csv(csv_path)),
        "rows": len(frame),
        "symbols_ever": len(kept_symbols),
        "excluded_symbols": list(frame.attrs.get("excluded_symbols", [])),
        "row_provenance": {
            str(r["symbol"]) + "|" + str(r["valid_from"]): {
                "source": r["_source"],
                "source_url": r["_source_url"],
                "notes": r["_notes"],
            }
            for r in frame.to_dict("records")
            if str(r["_source"]).strip()
        },
    }
    provenance_path = out_dir / "provenance.json"
    provenance_path.write_text(
        json.dumps(provenance, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return csv_path
