"""Real-data research assembly for the v0.7 baseline re-validation.

Sits on top of the validated **clean** layer (:class:`data.dataset.
CleanDataCatalog`) and the point-in-time universe
(:class:`data.universe.UniverseDataset`) and produces exactly what the
frozen v0.6 baseline needs:

* a rectangular date x symbol close panel over the *maximum clean
  overlapping period* (v0.7 §10: chosen by data availability, never by
  results);
* a boolean point-in-time membership mask (date x symbol) so the frozen
  ``MomentumQualityStrategy`` ranks only within each date's actual index
  members (v0.7 §8);
* the §7 data-completeness report (symbols requested/received, coverage,
  gaps, exclusions with reasons, adjustment coverage, source coverage).

Nothing here fills prices, re-orders timestamps, or mutates raw/clean
files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from data.dataset import CleanDataCatalog
from data.quality import TradingCalendar
from data.universe import UniverseDataset
from research.contracts import MarketData, ResearchInputError

__all__ = [
    "RealDataWindow",
    "ResearchPanels",
    "build_market_panels",
    "build_active_membership_panel",
    "market_calendar",
    "requested_constituents",
    "real_data_dataset_version",
    "load_fundamentals_bundle",
]


@dataclass(frozen=True)
class RealDataWindow:
    """The research window plus its trading calendar (data-derived)."""

    start: date
    end: date
    calendar: tuple[pd.Timestamp, ...] = ()

    @property
    def index(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.calendar)

    @property
    def trading_days(self) -> int:
        return len(self.calendar)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "trading_days": len(self.calendar),
        }


@dataclass(frozen=True)
class ResearchPanels:
    """Rectangular research panels plus the exclusion audit."""

    close: pd.DataFrame
    high: pd.DataFrame | None
    low: pd.DataFrame | None
    volume: pd.DataFrame | None
    window: RealDataWindow
    symbols: tuple[str, ...]
    requested_symbols: tuple[str, ...]
    excluded: Mapping[str, str] = field(default_factory=dict)
    market_holidays: tuple[str, ...] = ()
    special_sessions: tuple[str, ...] = ()
    #: Symbols recorded in ``excluded`` that are nevertheless *present* in the
    #: panel (AUDIT-014). A reader must not assume ``excluded`` == "not here".
    incomplete_symbols: tuple[str, ...] = ()
    #: How gaps were handled: "ffill_bfill" (prices imputed), "none" (NaN) or
    #: "none_excluded" (NaN, incomplete symbols dropped).
    price_fill: str = "ffill_bfill"

    @property
    def market_data(self) -> MarketData:
        """Return the frozen-contract :class:`MarketData` (close only,
        exactly as the v0.6 baseline consumes it)."""
        return MarketData(close=self.close)

    def symbols_frame(self) -> pd.DataFrame:
        """Long (symbol, first, last, observations) coverage frame."""
        records = []
        for symbol in self.symbols:
            series = self.close[symbol]
            records.append(
                {
                    "symbol": symbol,
                    "first_date": series.index[0].date().isoformat(),
                    "last_date": series.index[-1].date().isoformat(),
                    "observations": int(len(series)),
                }
            )
        return pd.DataFrame(records)


def _pivot_field(
    frames: Mapping[str, pd.DataFrame], field_name: str, calendar: Sequence
) -> pd.DataFrame:
    panels = []
    for symbol, frame in frames.items():
        panel = frame.sort_values("date").set_index("date")[field_name].to_frame(symbol)
        panels.append(panel)
    wide = pd.concat(panels, axis=1)
    wide = wide.reindex(pd.DatetimeIndex(calendar)).sort_index()
    return wide


def build_market_panels(
    catalog: CleanDataCatalog,
    requested_symbols: Sequence[str],
    *,
    source: str = "eod2_data",
    window_start: date | str,
    window_end: date | str,
    minimum_symbols: int = 50,
    exclude_incomplete: bool = True,
    fill_missing_prices: bool = False,
) -> ResearchPanels:
    """Assemble the rectangular research panel over the max clean window.

    Calendar = union of all observed dates within
    ``[window_start, window_end]``. A symbol is *complete* when it has an
    observation for every calendar day.

    AUDIT-014 (FIXED — the defaults no longer fabricate prices):

    * ``exclude_incomplete=True`` (default) drops a symbol that is missing
      any calendar day in the window and records the reason in ``excluded``.
      It used to default to ``False``, which kept the symbol *in* the panel
      while also listing it in ``excluded`` — so ``excluded`` was notes, not
      a list of removed symbols, and the completeness report's
      ``excluded_symbols`` block was factually wrong. Pass ``False`` to
      restore the historical behaviour; :attr:`ResearchPanels.incomplete_symbols`
      then names every symbol the panel contains despite being incomplete.
    * ``fill_missing_prices=False`` (default) leaves gaps as ``NaN``.
      It used to default to ``True``, which forward- **and back**-filled: a
      symbol that listed mid-window had its *first* traded price copied
      backwards over every earlier date, so the panel contained prices for
      days on which the instrument did not exist (verified in the repository's
      own fixture world: NEWCO, first trade 2024-03-05, carried a constant
      121.18 across the preceding 306 sessions). That contradicted
      :mod:`data.quality`'s "the system does not impute prices", and it is
      why :func:`backtest.engine.VectorBTResearchEngine.run` now refuses a
      panel with gaps instead of filling them (AUDIT-009).

    Behavioural note: flipping these defaults changes every published
    Sharpe/CAGR/drawdown produced from a panel that contained an incomplete
    symbol. That is the point — the old numbers were computed partly from
    prices that never existed — but any stored baseline must be re-derived.
    """
    start = pd.Timestamp(window_start).date()
    end = pd.Timestamp(window_end).date()
    if end < start:
        raise ResearchInputError("window_end must not be before window_start")

    frames: dict[str, pd.DataFrame] = {}
    missing_in_clean: dict[str, str] = {}
    for symbol in sorted(set(s.strip().upper() for s in requested_symbols)):
        try:
            frame, _ = catalog.read_clean(symbol, source=source)
        except FileNotFoundError:
            missing_in_clean[symbol] = "no validated (clean) data on file"
            continue
        frame = frame.copy()
        frame["date"] = pd.to_datetime(frame["date"])
        in_window = (frame["date"] >= pd.Timestamp(start)) & (
            frame["date"] <= pd.Timestamp(end)
        )
        windowed = frame.loc[in_window]
        if windowed.empty:
            missing_in_clean[symbol] = f"no observations inside window {start}..{end}"
            continue
        frames[symbol] = windowed

    if not frames:
        raise ResearchInputError(
            "no symbols have clean data inside the research window; "
            "run the ingestion step first"
        )

    calendar_set: set[pd.Timestamp] = set()
    for frame in frames.values():
        calendar_set |= set(pd.to_datetime(frame["date"]))
    calendar = tuple(sorted(calendar_set))
    window = RealDataWindow(start=start, end=end, calendar=calendar)

    complete: dict[str, pd.DataFrame] = {}
    excluded: dict[str, str] = {}
    excluded.update(missing_in_clean)
    incomplete: tuple[str, ...] = ()
    for symbol, frame in sorted(frames.items()):
        observed = set(pd.to_datetime(frame["date"]))
        missing_days = len(set(calendar) - observed)
        if missing_days:
            first = str(pd.to_datetime(frame["date"]).min().date())
            reason = (
                f"incomplete price history in window: {missing_days} "
                f"calendar day(s) missing (first observation {first})"
            )
            excluded[symbol] = reason
            if exclude_incomplete:
                # The documented contract: drop it from the panel and keep
                # the reason. Nothing is silently discarded.
                continue
            incomplete = incomplete + (symbol,)
        complete[symbol] = frame

    if len(complete) < minimum_symbols:
        raise ResearchInputError(
            f"only {len(complete)} symbols have a price history in the window "
            f"(minimum {minimum_symbols}); data is insufficient for the baseline"
        )

    symbols = tuple(sorted(complete))

    def _panel(field_name: str, *, zero_fill: bool = False) -> pd.DataFrame:
        wide = _pivot_field(complete, field_name, calendar)
        if zero_fill:
            return wide.fillna(0)
        if fill_missing_prices:
            return wide.ffill().bfill()
        return wide

    close = _panel("close")
    high = _panel("high")
    low = _panel("low")
    volume = _panel("volume", zero_fill=True)

    weekdays_not_traded = tuple(
        ts.date().isoformat() for ts in calendar if ts.dayofweek >= 5
    )
    return ResearchPanels(
        close=close,
        high=high,
        low=low,
        volume=volume,
        window=window,
        symbols=symbols,
        requested_symbols=tuple(
            sorted(set(s.strip().upper() for s in requested_symbols))
        ),
        excluded=excluded,
        market_holidays=weekdays_not_traded,
        incomplete_symbols=incomplete,
        price_fill=(
            "none_excluded"
            if exclude_incomplete
            else ("ffill_bfill" if fill_missing_prices else "none")
        ),
    )


def build_active_membership_panel(
    dataset: UniverseDataset,
    index_name: str,
    *,
    calendar: Sequence[pd.Timestamp],
    symbols: Sequence[str],
) -> pd.DataFrame:
    """Boolean date x symbol point-in-time membership mask.

    ``True`` exactly when the symbol was an index member on that date
    (``valid_from <= date <= valid_to`` or open-ended) AND is present in
    the research panel. Dates/symbols outside the panel are ``False`` —
    the mask can only restrict, never widen, the cross-section.
    """
    symbol_set = set(s.strip().upper() for s in symbols)
    ordered_symbols = sorted(symbol_set)
    records: dict[pd.Timestamp, set[str]] = {}
    for timestamp in calendar:
        members = set(dataset.members_at(index_name, timestamp.date()))
        records[timestamp] = members & symbol_set
    # Row values MUST follow the sorted column order — iterating the raw
    # set here would silently permute the membership columns (set order is
    # arbitrary).
    panel = pd.DataFrame(
        [
            [day in records[timestamp] for day in ordered_symbols]
            for timestamp in calendar
        ],
        index=pd.DatetimeIndex(list(calendar)),
        columns=ordered_symbols,
    )
    return panel


def market_calendar(
    frames: Mapping[str, pd.DataFrame],
    *,
    start: date | str,
    end: date | str,
) -> TradingCalendar:
    """Trading calendar derived from the data itself (holidays + special
    sessions included) for the quality layer's off-calendar checks."""
    calendar_set: set[date] = set()
    for frame in frames.values():
        series = pd.to_datetime(frame["date"])
        for ts in series:
            calendar_set.add(ts.date())
    frozen = frozenset(calendar_set)

    def is_trading_day(day: date) -> bool:
        return day in frozen

    return TradingCalendar(is_trading_day=is_trading_day)


def requested_constituents(
    dataset: UniverseDataset,
    *,
    window_start: date | str,
    as_of: date | str,
) -> list[str]:
    """Every symbol that was an index member at any point in the window.

    This is the "symbols requested" set for the §7 completeness report:
    membership rows overlapping ``[window_start, as_of]``.
    """
    start = pd.Timestamp(window_start)
    end = pd.Timestamp(as_of)
    symbols: set[str] = set()
    for member in dataset.for_index("nifty100"):
        valid_from = pd.Timestamp(member.valid_from)
        valid_to = (
            pd.Timestamp(member.valid_to) if member.valid_to is not None else None
        )
        if valid_from <= end and (valid_to is None or valid_to >= start):
            symbols.add(member.symbol)
    return sorted(symbols)


def real_data_dataset_version(
    eod2_spec: Any,
    membership_spec: Any,
    fundamentals_fingerprint: str = "pending-operator-bundle",
) -> str:
    """Version string tying a run to exact source snapshots (v0.7 §14)."""
    eod2_commit = str(getattr(eod2_spec, "commit", ""))[:12] or "unknown"
    membership_commit = str(getattr(membership_spec, "commit", ""))[:12] or "unknown"
    return (
        f"real-nifty100-v1 "
        f"(eod2_data@{eod2_commit} nse-membership@{membership_commit} "
        f"fundamentals@{fundamentals_fingerprint[:12]})"
    )


def frame_fingerprint(frame: pd.DataFrame) -> str:
    """Stable SHA-256 over a long frame (sorted, string-normalised)."""
    payload = frame.copy()
    for column in payload.columns:
        if payload[column].dtype == object:
            payload[column] = payload[column].astype(str)
    payload["date"] = pd.to_datetime(payload["date"]).astype("int64")
    blob = (
        payload.sort_values(["symbol", "date"], na_position="last")
        .to_records(index=False)
        .tobytes()
    )
    return hashlib.sha256(blob).hexdigest()


def load_fundamentals_bundle(
    bundle_dir: str | Path,
    *,
    as_of: date | str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load + validate the operator fundamentals bundle (point-in-time).

    Contract: long frame with ``date`` (availability date, i.e. the date
    the numbers were knowable — the source applies a conservative
    one-quarter publication lag), ``symbol``, ``roe``,
    ``debt_to_equity``. Rows are validated but never re-scaled: the
    quality factors rank cross-sectionally, so absolute scale is
    immaterial; magnitude sanity is reported, not repaired.
    """
    bundle = Path(bundle_dir)
    parquet_path = bundle / "fundamentals_quarterly.parquet"
    provenance_path = bundle / "fundamentals_provenance.json"
    if not parquet_path.is_file():
        raise FileNotFoundError(
            f"fundamentals bundle missing: {parquet_path}. Run the operator "
            "command: python scripts/ingest_real_data.py --fetch-fundamentals"
        )
    frame = pd.read_parquet(parquet_path)
    required = {"date", "symbol", "roe", "debt_to_equity"}
    missing = required - set(frame.columns)
    if missing:
        raise ResearchInputError(
            f"fundamentals bundle missing columns: {sorted(missing)}"
        )
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["roe"] = pd.to_numeric(frame["roe"], errors="coerce")
    frame["debt_to_equity"] = pd.to_numeric(frame["debt_to_equity"], errors="coerce")
    if frame["date"].isna().any():
        raise ResearchInputError("fundamentals bundle contains unparseable dates")
    # Availability is a *date* (the day the figures were knowable); any
    # intraday timestamp is normalised to the start of that day.
    frame["date"] = frame["date"].dt.normalize()
    # A row with at least one usable metric is kept (the composite quality
    # factor falls back to the cross-sectional median for a missing metric).
    frame = frame[frame[["roe", "debt_to_equity"]].notna().any(axis=1)]
    if frame.empty:
        raise ResearchInputError("fundamentals bundle has no usable metric rows")
    duplicates = frame.duplicated(subset=["date", "symbol"])
    if duplicates.any():
        raise ResearchInputError(
            f"fundamentals bundle has {int(duplicates.sum())} duplicate "
            "(date, symbol) rows"
        )
    provenance: dict[str, Any] = {}
    if provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    dropped_after_as_of = 0
    if as_of is not None:
        reference = pd.Timestamp(as_of).date()
        future = frame["date"].dt.date > reference
        if future.any():
            # Availability dates after the as-of reference are information
            # that was not knowable yet: they are dropped (and counted),
            # never used. A bundle snapshot legitimately contains upcoming
            # availability dates, so this is a normal trim, not an error.
            dropped_after_as_of = int(future.sum())
            frame = frame[~future].copy()
            if frame.empty:
                raise ResearchInputError(
                    f"fundamentals bundle has no rows available on or before "
                    f"as-of {reference} ({dropped_after_as_of} future-"
                    "availability rows dropped)"
                )
        provenance["dropped_after_as_of"] = dropped_after_as_of
    return frame, provenance
