"""Configuration-driven research universes and frozen Nifty constituent snapshots.

The frozen ``nifty_50`` / ``nifty_100`` snapshots remain the lightweight
single-date research universe. For *historical* index membership with
validity windows and survivorship-bias protection use
``build_universe_from_dataset`` (backed by ``data.universe.UniverseDataset``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .contracts import ResearchInputError

# These are explicit research snapshots, not a claim that constituents remain
# current. A production run should record the chosen snapshot in its experiment.
_NIFTY_50 = (
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BEL",
    "BHARTIARTL",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "ETERNAL",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "INDUSINDBK",
    "INFY",
    "ITC",
    "JIOFIN",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TCS",
    "TECHM",
    "TITAN",
    "TRENT",
    "ULTRACEMCO",
    "WIPRO",
)
_NIFTY_100_ADDITIONS = (
    "ABB",
    "ADANIENSOL",
    "ADANIGREEN",
    "AMBUJACEM",
    "BANKBARODA",
    "BDL",
    "BHEL",
    "BOSCHLTD",
    "BPCL",
    "CANBK",
    "CHOLAFIN",
    "COLPAL",
    "DABUR",
    "DIVISLAB",
    "DLF",
    "DMART",
    "GAIL",
    "GLAND",
    "GODREJCP",
    "GODREJPROP",
    "HAL",
    "HAVELLS",
    "ICICIGI",
    "ICICIPRULI",
    "IDFCFIRSTB",
    "INDHOTEL",
    "IOC",
    "IRCTC",
    "JINDALSTEL",
    "LODHA",
    "LUPIN",
    "MARICO",
    "MAXHEALTH",
    "MOTHERSON",
    "NHPC",
    "PERSISTENT",
    "PIDILITIND",
    "PNB",
    "POLYCAB",
    "RECLTD",
    "SAIL",
    "SBICARD",
    "SHREECEM",
    "SIEMENS",
    "TATAPOWER",
    "TORNTPOWER",
    "TVSMOTOR",
    "VBL",
    "VEDL",
    "ZYDUSLIFE",
)


@dataclass(frozen=True, slots=True)
class Universe:
    """Immutable named set of research symbols and its constituent metadata."""

    name: str
    symbols: tuple[str, ...]
    as_of: date | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ResearchInputError("universe name must be non-empty")
        if any(not isinstance(symbol, str) for symbol in self.symbols):
            raise ResearchInputError("universe symbols must be strings")
        symbols = tuple(symbol.strip().upper() for symbol in self.symbols)
        if any(not symbol for symbol in symbols):
            raise ResearchInputError("universe symbols must be non-empty")
        if self.as_of is not None and not isinstance(self.as_of, date):
            raise ResearchInputError("universe as_of must be a date")
        if len(set(symbols)) != len(symbols):
            raise ResearchInputError("universe symbols must be unique")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_symbols(
        cls,
        name: str,
        symbols: Sequence[str],
        *,
        as_of: date | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Universe:
        """Create a named universe from a sequence of canonical symbols."""
        return cls(name, tuple(symbols), as_of=as_of, metadata=metadata or {})

    def contains(self, symbol: str) -> bool:
        """Return whether the universe contains ``symbol`` case-insensitively."""
        return symbol.strip().upper() in self.symbols

    @property
    def history(self) -> list[tuple[str, ...]]:
        """Return the survivorship-safe membership history for this universe.

        Frozen snapshots are a single-date view, so this returns a single
        period of membership. Dataset-built universes carry the full
        ``UniverseDataset`` in metadata when constructed via
        ``build_universe_from_dataset``, which can resolve per-date history.
        The backtest engine requires an explicit ``universe_history`` to
        refuse running today's universe over the past.
        """
        return [self.symbols]

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable universe definition."""
        return {
            "name": self.name,
            "symbols": list(self.symbols),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "metadata": dict(self.metadata),
        }


def nifty_50(*, as_of: date | None = None) -> Universe:
    """Return the frozen 50-symbol Nifty research snapshot."""
    return Universe.from_symbols(
        "nifty50",
        _NIFTY_50,
        as_of=as_of,
        metadata={"index": "NIFTY 50", "snapshot": "repository"},
    )


def nifty_100(*, as_of: date | None = None) -> Universe:
    """Return the frozen 100-symbol Nifty research snapshot."""
    return Universe.from_symbols(
        "nifty100",
        _NIFTY_50 + _NIFTY_100_ADDITIONS,
        as_of=as_of,
        metadata={"index": "NIFTY 100", "snapshot": "repository"},
    )


def custom_universe(
    symbols: Sequence[str],
    name: str = "custom",
    *,
    as_of: date | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Universe:
    """Create a configuration-driven custom research universe."""
    return Universe.from_symbols(name, symbols, as_of=as_of, metadata=metadata)


def resolve_universe(config: Mapping[str, Any] | Path | str) -> Universe:
    """Resolve a built-in or custom universe from a mapping or JSON file.

    Accepted mapping forms are ``{"name": "nifty50"}``,
    ``{"name": "nifty100"}``, and ``{"name": "custom", "symbols": [...]}``.
    """
    if isinstance(config, Path | str):
        path = Path(config).expanduser()
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResearchInputError(
                f"universe configuration does not exist: {path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ResearchInputError(
                f"universe configuration is not valid JSON: {path}"
            ) from exc
    if not isinstance(config, Mapping):
        raise ResearchInputError(
            "universe configuration must be a mapping or JSON path"
        )
    name = str(config.get("name", "custom")).strip().lower().replace("_", "")
    as_of_raw = config.get("as_of")
    try:
        as_of = (
            date.fromisoformat(as_of_raw) if isinstance(as_of_raw, str) else as_of_raw
        )
    except ValueError as exc:
        raise ResearchInputError("universe as_of must be an ISO date") from exc
    metadata = config.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ResearchInputError("universe metadata must be a mapping")
    if name in {"nifty50", "nifty"}:
        return nifty_50(as_of=as_of)
    if name == "nifty100":
        return nifty_100(as_of=as_of)
    raw_symbols = config.get("symbols", ())
    if not isinstance(raw_symbols, Sequence) or isinstance(raw_symbols, str):
        raise ResearchInputError("custom universe symbols must be a sequence")
    return custom_universe(
        raw_symbols, name=name or "custom", as_of=as_of, metadata=metadata
    )


def _as_date(value: Any, field_name: str) -> date | None:
    """Coerce a date-compatible value (None, ISO string, date, datetime)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ResearchInputError(
                f"universe {field_name} must be an ISO date"
            ) from exc
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(f"universe {field_name} is not a valid date") from exc


def build_universe_from_dataset(
    dataset: Any,
    index_name: str,
    *,
    as_of: date | str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Universe:
    """Build a point-in-time :class:`Universe` from a historical dataset.

    ``dataset`` is a ``data.universe.UniverseDataset`` (or anything exposing
    ``members_at(index_name, day)``, ``all_symbols(index_name)`` and
    ``validate_period(index_name, start, end)``). The returned universe's
    metadata records the dataset's validity window so downstream backtests
    can refuse periods the dataset does not cover.

    ``as_of`` defaults to the dataset's earliest recorded membership date so
    the universe resolves to the fullest survivorship-safe history.
    """
    from data.universe import UniverseDataset

    if not isinstance(dataset, UniverseDataset):
        raise ResearchInputError(
            "dataset must be a data.universe.UniverseDataset instance"
        )
    canonical_index = str(index_name).strip().lower().replace("_", "").replace(" ", "")
    if canonical_index not in {"nifty50", "nifty100", "nifty500"}:
        raise ResearchInputError(f"unsupported index: {index_name}")

    earliest, latest = dataset.valid_window(index_name)
    target = _as_date(as_of, "as_of") or earliest
    if target < earliest:
        raise ResearchInputError(
            f"as_of {target.isoformat()} predates universe membership "
            f"{earliest.isoformat()} for {index_name!r}"
        )
    if latest is not None and target > latest:
        raise ResearchInputError(
            f"as_of {target.isoformat()} exceeds universe membership "
            f"{latest.isoformat()} for {index_name!r}"
        )
    symbols = dataset.members_at(index_name, target)
    merged = {
        "index": index_name,
        "snapshot": "dataset",
        "valid_from": earliest.isoformat(),
        "valid_to": latest.isoformat() if latest is not None else None,
        "all_symbols_count": len(dataset.all_symbols(index_name)),
    }
    if metadata:
        merged.update(dict(metadata))
    universe_size = (
        100 if "100" in canonical_index else 500 if "500" in canonical_index else 50
    )
    return Universe.from_symbols(
        f"nifty{universe_size}",
        symbols,
        as_of=target,
        metadata=merged,
    )


def ensure_universe_period_covers(
    universe: Universe,
    start: date | str | None,
    end: date | str | None,
) -> None:
    """Refuse a backtest period that the universe dataset does not cover.

    Only universes built from a dataset (which carry ``valid_from`` /
    ``valid_to`` metadata) are validated. Frozen snapshots carry no validity
    window and are left to the caller — they are a single-date view, not a
    historical membership claim.
    """
    valid_from_raw = universe.metadata.get("valid_from")
    if valid_from_raw is None:
        return
    valid_from = _as_date(valid_from_raw, "valid_from")
    valid_to = _as_date(universe.metadata.get("valid_to"), "valid_to")
    if start is None and end is None:
        return
    earliest_backtest = _as_date(start, "start") if start is not None else None
    latest_backtest = _as_date(end, "end") if end is not None else None
    if earliest_backtest is not None and earliest_backtest < valid_from:
        raise ResearchInputError(
            f"universe {universe.name!r} has no membership before "
            f"{valid_from.isoformat()}; requested backtest from "
            f"{earliest_backtest.isoformat()}"
        )
    if (
        latest_backtest is not None
        and valid_to is not None
        and latest_backtest > valid_to
    ):
        raise ResearchInputError(
            f"universe {universe.name!r} has no membership after "
            f"{valid_to.isoformat()}; requested backtest to "
            f"{latest_backtest.isoformat()}"
        )
