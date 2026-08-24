"""Validated OHLCV handling and data-quality checks.

Design rules:

* Invalid records are rejected and reported — never silently dropped into
  "good" data without a trace, and never silently filled.
* Real market gaps (holidays, trading halts) are reported as missing
  candles; the caller decides. The system does not impute prices.
* All checks are deterministic and pure functions of the input frame.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

from models.domain import MarketBar

__all__ = [
    "DataQualityReport",
    "QualityIssue",
    "check_ohlcv_long_frame",
    "detect_data_staleness",
    "load_market_bars",
    "validate_market_bars",
]

_REQUIRED_COLUMNS = ("date", "symbol", "open", "high", "low", "close")

#: Canonical long-form column names (case-insensitive on input).
_CANONICAL = {
    "date": "date",
    "symbol": "symbol",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "adj_close": "adj_close",
    "is_adjusted": "is_adjusted",
    "corp_action_applied": "corp_action_applied",
    "ingested_at": "ingested_at",
    "source_ts": "source_ts",
    "source": "source",
    "exchange": "exchange",
}


class DataQualityError(ValueError):
    """Raised when market data is structurally unusable."""


@dataclass(frozen=True)
class QualityIssue:
    """One detected quality problem."""

    kind: str
    symbol: str | None = None
    date: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class DataQualityReport:
    """Outcome of validating a market-data frame."""

    total_rows: int
    accepted_rows: int
    issues: tuple[QualityIssue, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        return not self.issues

    def issues_by_kind(self) -> dict[str, list[QualityIssue]]:
        grouped: dict[str, list[QualityIssue]] = {}
        for issue in self.issues:
            grouped.setdefault(issue.kind, []).append(issue)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "accepted_rows": self.accepted_rows,
            "issues": [
                {
                    "kind": i.kind,
                    "symbol": i.symbol,
                    "date": i.date,
                    "detail": i.detail,
                }
                for i in self.issues
            ],
        }


def _canonical_columns(frame: pd.DataFrame) -> pd.DataFrame:
    mapping = {}
    for column in frame.columns:
        key = str(column).strip().lower()
        if key in _CANONICAL:
            mapping[column] = _CANONICAL[key]
    renamed = frame.rename(columns=mapping)
    missing = [name for name in _REQUIRED_COLUMNS if name not in renamed.columns]
    if missing:
        raise DataQualityError(f"market data is missing columns: {missing}")
    return renamed


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def check_ohlcv_long_frame(
    frame: pd.DataFrame,
    *,
    source: str = "unknown",
    exchange: str = "NSE",
) -> tuple[pd.DataFrame, DataQualityReport]:
    """Validate a long-form OHLCV frame row by row.

    Returns ``(accepted, report)``. Invalid rows are excluded from
    ``accepted`` and recorded in ``report``. Rows are never filled or
    imputed; missing candles are reported, not created.

    Checks per row: finite positive prices, ``high >= max(open, close)``,
    ``low <= min(open, close)``, ``high >= low``, ``volume >= 0``, unique
    ``(date, symbol)``, parseable/sorted-free timestamps.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise DataQualityError("market data frame is empty")
    working = _canonical_columns(frame)

    issues: list[QualityIssue] = []
    total = len(working)
    accepted_mask = pd.Series(True, index=working.index)

    dates = pd.to_datetime(working["date"], errors="coerce")
    bad_dates = dates.isna()
    issues.extend(
        QualityIssue(
            "invalid_timestamp",
            symbol=str(working.at[i, "symbol"]),
            detail="date could not be parsed",
        )
        for i in working.index[bad_dates]
    )
    accepted_mask &= ~bad_dates

    symbols = working["symbol"].astype(str).str.strip().str.upper()
    if (symbols == "").any():
        issues.append(QualityIssue("invalid_symbol", detail="empty symbol value"))
        accepted_mask &= symbols.ne("")

    for field_name in ("open", "high", "low", "close"):
        values = pd.to_numeric(working[field_name], errors="coerce")
        bad = accepted_mask & (
            values.isna() | (values <= 0) | values.map(lambda v: not math.isfinite(v))
        )
        if bad.any():
            issues.append(
                QualityIssue(
                    f"invalid_{field_name}",
                    detail=(
                        f"{int(bad.sum())} rows with missing/non-positive/"
                        f"non-finite {field_name}"
                    ),
                )
            )
            accepted_mask &= ~bad

    volume = pd.to_numeric(
        working.get("volume", pd.Series(0.0, index=working.index)), errors="coerce"
    )
    bad_volume = accepted_mask & (volume.isna() | (volume < 0))
    if bad_volume.any():
        issues.append(
            QualityIssue(
                "invalid_volume",
                detail=f"{int(bad_volume.sum())} rows with negative or missing volume",
            )
        )
        accepted_mask &= ~bad_volume

    valid = working.loc[accepted_mask].copy()
    valid["date"] = dates[accepted_mask]
    high = pd.to_numeric(valid["high"], errors="coerce")
    low = pd.to_numeric(valid["low"], errors="coerce")

    ohlc_bad = (
        (high < valid[["open", "close"]].max(axis=1))
        | (low > valid[["open", "close"]].min(axis=1))
        | (high < low)
    )
    if ohlc_bad.any():
        for index in valid.index[ohlc_bad]:
            issues.append(
                QualityIssue(
                    "ohlc_inconsistency",
                    symbol=str(symbols.loc[index]),
                    date=str(dates.loc[index].date())
                    if pd.notna(dates.loc[index])
                    else None,
                    detail="high/low/open/close are mutually inconsistent",
                )
            )
        valid = valid.loc[~ohlc_bad]

    duplicates = valid.duplicated(subset=["date", "symbol"], keep=False)
    if duplicates.any():
        for index in valid.index[duplicates]:
            issues.append(
                QualityIssue(
                    "duplicate_row",
                    symbol=str(symbols.loc[index]),
                    date=str(dates.loc[index].date())
                    if pd.notna(dates.loc[index])
                    else None,
                    detail="duplicate (date, symbol) row",
                )
            )
        valid = valid.loc[~duplicates]

    accepted = valid.reset_index(drop=True).copy()
    accepted["symbol"] = symbols.loc[accepted.index]
    accepted["date"] = dates.loc[accepted.index]
    for extra in (
        "volume",
        "adj_close",
        "source",
        "exchange",
        "ingested_at",
        "source_ts",
    ):
        if extra in accepted.columns:
            continue
        if extra in working.columns:
            accepted[extra] = working[accepted.index][extra]
        elif extra in ("volume",):
            accepted[extra] = 0.0
    if "source" not in accepted.columns:
        accepted["source"] = source
    if "exchange" not in accepted.columns:
        accepted["exchange"] = exchange
    if "volume" not in accepted.columns:
        accepted["volume"] = 0.0

    return accepted, DataQualityReport(
        total_rows=total, accepted_rows=len(accepted), issues=tuple(issues)
    )


def load_market_bars(
    frame: pd.DataFrame,
    *,
    source: str = "unknown",
    exchange: str = "NSE",
) -> tuple[list[MarketBar], DataQualityReport]:
    """Validate a long frame into strict :class:`MarketBar` records.

    Rows that fail strict ``MarketBar`` validation are reported as issues
    instead of raising, so one bad row does not destroy a valid dataset.
    """
    accepted, report = check_ohlcv_long_frame(frame, source=source, exchange=exchange)
    issues = list(report.issues)
    bars: list[MarketBar] = []
    for _, row in accepted.iterrows():
        payload: dict[str, Any] = {
            "source": row.get("source", source),
            "symbol": row["symbol"],
            "exchange": row.get("exchange", exchange),
            "date": pd.Timestamp(row["date"]).date(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0.0) or 0.0),
        }
        if "adj_close" in row and pd.notna(row.get("adj_close")):
            payload["adj_close"] = float(row["adj_close"])
        try:
            bars.append(MarketBar.model_validate(payload))
        except Exception:
            issues.append(
                QualityIssue(
                    "strict_validation_failed",
                    symbol=str(row["symbol"]),
                    date=str(pd.Timestamp(row["date"]).date()),
                    detail="row failed strict MarketBar validation",
                )
            )
    return bars, DataQualityReport(
        total_rows=report.total_rows,
        accepted_rows=len(bars),
        issues=tuple(issues),
    )


def detect_data_staleness(
    frame: pd.DataFrame | Iterable[pd.Timestamp],
    *,
    reference_now: datetime | None = None,
    max_staleness_days: float = 6.0,
) -> QualityIssue | None:
    """Report when the latest observation is older than the allowed age.

    Weekends/holidays make a 6-day default safe for daily data; tighten for
    intraday. Returns None when fresh.
    """
    if isinstance(frame, pd.DataFrame):
        if "date" not in {str(c).lower() for c in frame.columns} or frame.empty:
            raise DataQualityError(
                "stale check requires a non-empty frame with a date column"
            )
        series = pd.to_datetime(frame["date"], errors="coerce")
    else:
        series = pd.DatetimeIndex(list(frame))
    series = series.dropna()
    if series.empty:
        return QualityIssue("staleness", detail="no parsable dates; data is unusable")
    latest = series.max().to_pydatetime()
    now = reference_now or datetime.now()
    if latest.tzinfo is not None and now.tzinfo is None:
        from datetime import timezone

        now = now.replace(tzinfo=timezone.utc)
    if latest.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    age_days = (now - latest).total_seconds() / 86400.0
    if age_days > max_staleness_days:
        return QualityIssue(
            "staleness",
            detail=f"latest observation {latest.date()} is {age_days:.1f} days old "
            f"(limit {max_staleness_days:.1f})",
        )
    return None


def detect_missing_candles(
    accepted: pd.DataFrame,
    *,
    tolerance_gap_days: int = 10,
) -> list[QualityIssue]:
    """Report per-symbol gaps and unaligned symbols.

    The expected calendar is the union of all observed dates, so a symbol
    missing a date that every other symbol has is a gap. Very large gaps
    (beyond ``tolerance_gap_days``) are flagged as long gaps. No candles are
    filled.
    """
    if accepted.empty:
        return []
    dates = pd.to_datetime(accepted["date"])
    symbols = accepted["symbol"].astype(str).str.upper()
    full_calendar = pd.DatetimeIndex(sorted(set(dates)))
    issues: list[QualityIssue] = []
    for symbol, symbol_dates in dates.groupby(symbols):
        present = pd.DatetimeIndex(sorted(set(symbol_dates)))
        missing = full_calendar.difference(present)
        for missing_date in missing:
            # Flag dates after the symbol's first candle (including after its
            # last one — a symbol that stopped reporting is a gap). Dates
            # before the first candle mean "not yet listed", not a gap.
            if present.min() < missing_date:
                issues.append(
                    QualityIssue(
                        "missing_candle",
                        symbol=str(symbol),
                        date=str(missing_date.date()),
                        detail="date present for other symbols but missing here",
                    )
                )
        if len(present) >= 2:
            gaps = pd.Series(present).diff().dropna()
            for gap in gaps:
                if gap.days > tolerance_gap_days:
                    issues.append(
                        QualityIssue(
                            "long_gap",
                            symbol=str(symbol),
                            detail=f"gap of {gap.days} calendar days",
                        )
                    )
    return issues


def validate_market_bars(
    frame: pd.DataFrame,
    *,
    source: str = "unknown",
    exchange: str = "NSE",
    reference_now: datetime | None = None,
    max_staleness_days: float = 6.0,
) -> tuple[pd.DataFrame, DataQualityReport]:
    """Full validation pipeline: rows, staleness, gaps; never fills data."""
    accepted, report = check_ohlcv_long_frame(frame, source=source, exchange=exchange)
    extra: list[QualityIssue] = list(report.issues)
    staleness = detect_data_staleness(
        accepted, reference_now=reference_now, max_staleness_days=max_staleness_days
    )
    if staleness is not None:
        extra.append(staleness)
    extra.extend(detect_missing_candles(accepted))
    return accepted, DataQualityReport(
        total_rows=report.total_rows,
        accepted_rows=len(accepted),
        issues=tuple(extra),
    )
