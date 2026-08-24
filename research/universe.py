"""Configuration-driven research universes and frozen Nifty constituent snapshots."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

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
            raise ResearchInputError(f"universe configuration does not exist: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ResearchInputError(f"universe configuration is not valid JSON: {path}") from exc
    if not isinstance(config, Mapping):
        raise ResearchInputError("universe configuration must be a mapping or JSON path")
    name = str(config.get("name", "custom")).strip().lower().replace("_", "")
    as_of_raw = config.get("as_of")
    try:
        as_of = date.fromisoformat(as_of_raw) if isinstance(as_of_raw, str) else as_of_raw
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
    return custom_universe(raw_symbols, name=name or "custom", as_of=as_of, metadata=metadata)
