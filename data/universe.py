"""Historical index-membership universe dataset.

This is the single source of truth for *historical* index membership
(Nifty 50 / Nifty 100 / Nifty 500) used by research backtests. Each
constituent is recorded with a validity window (``valid_from`` /
``valid_to``) so a backtest can resolve exactly which symbols were members
on a given date.

Survivorship-bias protection is structural, not optional:

* Delisted / removed constituents are kept in the dataset with a finite
  ``valid_to`` — they are never silently dropped, so a historical backtest
  still "sees" and can penalise the names that later left the index.
* ``UniverseDataset.members_at`` resolves membership for a specific date,
  and ``UniverseDataset.validate_period`` refuses to backtest across a span
  that the dataset does not cover with explicit membership.

The dataset is loaded from simple CSV/Parquet rows so it can be regenerated
from an authoritative source (NSE index factsheets, bseindia) without code
changes. See ``data/universe/README.md`` for provenance and regeneration.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

__all__ = [
    "UniverseMembership",
    "UniverseDataset",
    "load_universe_dataset",
    "universe_dataset_dir",
]

_INDEX_ALIASES: Mapping[str, str] = {
    "nifty50": "nifty50",
    "nifty_50": "nifty50",
    "nifty 50": "nifty50",
    "nifty100": "nifty100",
    "nifty_100": "nifty100",
    "nifty 100": "nifty100",
    "nifty500": "nifty500",
    "nifty_500": "nifty500",
    "nifty 500": "nifty500",
}


def _canonical_index(index_name: str) -> str:
    key = index_name.strip().lower()
    if key not in _INDEX_ALIASES:
        raise ValueError(
            f"unsupported index {index_name!r}; expected nifty50/nifty100/nifty500"
        )
    return _INDEX_ALIASES[key]


def _optional_date(value: Any) -> _dt.date | None:
    if value is None:
        return None
    if pd.isna(value):
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    try:
        return pd.Timestamp(value).date()
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid universe date {value!r}") from exc


def _parse_date(value: Any) -> _dt.date:
    parsed = _optional_date(value)
    if parsed is None:
        raise ValueError("date value is required")
    return parsed


@dataclass(frozen=True, slots=True)
class UniverseMembership:
    """One constituent's membership window within an index.

    ``valid_to`` is ``None`` while the constituent is currently a member
    (open-ended). ``delisted`` flags names removed from the exchange; such
    rows must remain in the dataset for survivorship-bias protection.
    """

    symbol: str
    index_name: str
    valid_from: _dt.date
    valid_to: _dt.date | None = None
    isin: str | None = None
    sector: str | None = None
    exchange: str = "NSE"
    delisted: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("universe membership symbol must be non-empty")
        symbol = self.symbol.strip().upper()
        index_name = _canonical_index(self.index_name)
        exchange = self.exchange.strip().upper() or "NSE"
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError(
                f"valid_to before valid_from for {symbol} in {index_name}"
            )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "index_name", index_name)
        object.__setattr__(self, "exchange", exchange)
        if self.isin is not None and not pd.isna(self.isin):
            object.__setattr__(self, "isin", self.isin.strip().upper() or None)
        else:
            object.__setattr__(self, "isin", None)
        if self.sector is not None and not pd.isna(self.sector):
            object.__setattr__(self, "sector", self.sector.strip())
        else:
            object.__setattr__(self, "sector", None)

    def is_member_on(self, day: _dt.date) -> bool:
        """Return whether this constituent was a member on ``day``."""
        if day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable membership record."""
        return {
            "symbol": self.symbol,
            "index_name": self.index_name,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "isin": self.isin,
            "sector": self.sector,
            "exchange": self.exchange,
            "delisted": self.delisted,
        }


def _coerce_row(row: Mapping[str, Any]) -> UniverseMembership:
    valid_from = _parse_date(row.get("valid_from"))
    valid_to = _optional_date(row.get("valid_to"))
    return UniverseMembership(
        symbol=str(row.get("symbol", "")),
        index_name=str(row.get("index_name", "")),
        valid_from=valid_from,
        valid_to=valid_to,
        isin=row.get("isin"),
        sector=row.get("sector"),
        exchange=str(row.get("exchange", "NSE")),
        delisted=bool(row.get("delisted", False)),
    )


class UniverseDataset:
    """An immutable set of historical index-membership records."""

    def __init__(self, members: Sequence[UniverseMembership]) -> None:
        if not members:
            raise ValueError("universe dataset must contain at least one membership")
        self.members = tuple(members)
        for member in self.members:
            if not isinstance(member, UniverseMembership):
                raise TypeError("members must be UniverseMembership instances")
        symbols = {member.symbol for member in self.members}
        if any(not symbol for symbol in symbols):
            raise ValueError("universe dataset contains an empty symbol")
        self._by_index: dict[str, tuple[UniverseMembership, ...]] = {}
        for index_name in _INDEX_ALIASES.values():
            self._by_index[index_name] = tuple(
                member
                for member in self.members
                if member.index_name == index_name
            )

    @property
    def index_names(self) -> tuple[str, ...]:
        """Return the indices represented in this dataset."""
        return tuple(sorted(self._by_index))

    def for_index(self, index_name: str) -> tuple[UniverseMembership, ...]:
        """Return all membership records for one index (including delisted)."""
        return self._by_index[_canonical_index(index_name)]

    def all_symbols(self, index_name: str | None = None) -> tuple[str, ...]:
        """Return every symbol ever in ``index_name`` (survivorship-safe)."""
        records = (
            self.members if index_name is None else self.for_index(index_name)
        )
        return tuple(sorted({member.symbol for member in records}))

    def members_at(
        self, index_name: str, day: _dt.date | str | pd.Timestamp
    ) -> tuple[str, ...]:
        """Resolve the exact member symbols on a given date.

        Delisted names simply do not appear on dates after their
        ``valid_to``; they are still available in ``all_symbols`` and in
        the full history so a backtest can correctly penalise them before
        removal. Never assumes a frozen "today" universe for the past.
        """
        target = _parse_date(day)
        return tuple(
            sorted(
                member.symbol
                for member in self.for_index(index_name)
                if member.is_member_on(target)
            )
        )

    def history(self, index_name: str, day: _dt.date) -> tuple[str, ...]:
        """Alias of :meth:`members_at` for engine ``universe_history`` use."""
        return self.members_at(index_name, day)

    def valid_window(
        self, index_name: str
    ) -> tuple[_dt.date, _dt.date | None]:
        """Return the union validity window of membership for an index."""
        records = self.for_index(index_name)
        if not records:
            raise ValueError(f"no membership records for index {index_name!r}")
        earliest = min(member.valid_from for member in records)
        latest: _dt.date | None = None
        open_ended = False
        for member in records:
            if member.valid_to is None:
                open_ended = True
            elif latest is None or member.valid_to > latest:
                latest = member.valid_to
        return earliest, latest if not open_ended else None

    def validate_period(
        self, index_name: str, start: _dt.date, end: _dt.date | None = None
    ) -> None:
        """Raise ``ValueError`` when a backtest period has no membership data.

        This is the "refuse invalid universe dates" guard. A backtest that
        asks for dates before any recorded membership — or after a closed
        dataset's last recorded removal — must be rejected rather than
        silently backfilled with today's constituents.
        """
        if end is not None and end < start:
            raise ValueError("end must not be before start")
        earliest, latest = self.valid_window(index_name)
        if start < earliest:
            raise ValueError(
                f"universe {index_name!r} has no membership before "
                f"{earliest.isoformat()} (requested {start.isoformat()})"
            )
        if latest is not None and (end or start) > latest:
            raise ValueError(
                f"universe {index_name!r} has no membership after "
                f"{latest.isoformat()} (requested "
                f"{(end or start).isoformat()})"
            )

    def to_frame(self) -> pd.DataFrame:
        """Return the full membership history as a DataFrame."""
        return pd.DataFrame([member.to_dict() for member in self.members])

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> "UniverseDataset":
        """Build a dataset from a DataFrame with membership columns."""
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            raise ValueError("universe frame must be non-empty")
        required = {"symbol", "index_name", "valid_from"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(
                f"universe frame missing columns: {sorted(missing)}"
            )
        return cls([_coerce_row(row) for row in frame.to_dict("records")])

    @classmethod
    def from_dir(cls, directory: str | Path) -> "UniverseDataset":
        """Load and merge every CSV/Parquet membership file in a directory."""
        path = Path(directory).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"universe dataset directory missing: {path}")
        records: list[UniverseMembership] = []
        for child in sorted(path.iterdir()):
            if child.suffix.lower() == ".csv":
                records.extend(cls.from_frame(pd.read_csv(child)).members)
            elif child.suffix.lower() == ".parquet":
                records.extend(cls.from_frame(pd.read_parquet(child)).members)
        if not records:
            raise ValueError(f"no universe membership files found in {path}")
        return cls(records)

    @classmethod
    def from_files(
        cls, paths: Sequence[str | Path]
    ) -> "UniverseDataset":
        """Load and merge membership from an explicit set of files."""
        records: list[UniverseMembership] = []
        for path in paths:
            path = Path(path).expanduser()
            if path.suffix.lower() == ".csv":
                records.extend(cls.from_frame(pd.read_csv(path)).members)
            elif path.suffix.lower() == ".parquet":
                records.extend(cls.from_frame(pd.read_parquet(path)).members)
            else:
                raise ValueError(f"unsupported universe file type: {path}")
        if not records:
            raise ValueError("no universe membership records loaded")
        return cls(records)


def universe_dataset_dir() -> Path:
    """Return the repository's ``data/universe`` directory."""
    return Path(__file__).resolve().parent / "universe"


def load_universe_dataset(
    path: str | Path | None = None,
) -> UniverseDataset:
    """Load the default repository universe dataset (or a custom directory)."""
    directory = Path(path).expanduser() if path is not None else universe_dataset_dir()
    return UniverseDataset.from_dir(directory)
