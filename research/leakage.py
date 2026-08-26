"""Leakage red-team: deterministic audits for look-ahead and contamination.

Each audit answers one yes/no question with evidence. Audits are
deterministic: same inputs, same verdict. They never mutate the inputs and
never repair a finding — a flagged issue must be fixed in code, then the
audit re-run.

Audits:

* ``audit_lookahead`` — recompute a factor (or signal) on truncated
  histories and compare with the full-data value at each date. Any
  difference means the computation used information from after that date.
* ``audit_future_availability`` — count fundamentals rows whose
  availability date is after the as-of reference.
* ``audit_rank_mask_order`` — verify that a quantile strategy never
  selects a symbol that was not a point-in-time member on the selection
  date (mask-before-rank). A rank-then-mask implementation WOULD select
  non-members and is detected here.
* ``audit_survivorship`` — inspect the membership panel against the price
  panel for delisting/eligibility anomalies.
* ``audit_holdout_isolation`` — verify development and holdout windows
  are disjoint in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from .contracts import ResearchInputError

__all__ = [
    "LeakageAudit",
    "audit_future_availability",
    "audit_holdout_isolation",
    "audit_lookahead",
    "audit_rank_mask_order",
    "audit_survivorship",
]


def audit_lookahead(
    compute: Callable[[pd.DataFrame], pd.DataFrame],
    close: pd.DataFrame,
    *,
    sample_dates: Sequence[pd.Timestamp] | None = None,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    """Detect future-information leakage in a factor computation.

    ``compute`` is a pure callable taking a price panel and returning a
    same-indexed factor panel. For a sample of dates, the factor is
    recomputed on the history truncated at that date and compared with the
    full-data factor at that date. A leaky computation (e.g. one that uses
    tomorrow's close) differs on the truncated recomputation.

    The recomputation is deterministic and O(samples x compute cost).
    """
    if not isinstance(close, pd.DataFrame) or close.empty:
        raise ResearchInputError("close must be a non-empty DataFrame")
    if not callable(compute):
        raise ResearchInputError("compute must be callable")
    full = compute(close)
    if not isinstance(full, pd.DataFrame) or not full.index.equals(close.index):
        raise ResearchInputError("compute must return a close-indexed panel")
    if sample_dates is None:
        # Default: every 21st observation plus the final date (bounded cost).
        positions = list(range(0, len(close), 21))
        if positions[-1] != len(close) - 1:
            positions.append(len(close) - 1)
        sample_dates = [close.index[pos] for pos in positions]
    violations: list[dict[str, Any]] = []
    for date in sample_dates:
        position = close.index.get_loc(date)
        if isinstance(position, slice):
            continue
        truncated = close.iloc[: position + 1]
        recomputed = compute(truncated)
        full_value = full.loc[date]
        truncated_value = recomputed.iloc[-1]
        difference = (full_value - truncated_value).abs()
        max_diff = float(difference.max()) if len(difference) else 0.0
        if max_diff > tolerance:
            violations.append(
                {
                    "date": date.isoformat(),
                    "max_abs_difference": max_diff,
                    "worst_symbol": str(difference.idxmax()),
                }
            )
    return {
        "audit": "lookahead",
        "sample_size": len(sample_dates),
        "violations": violations,
        "clean": not violations,
        "note": (
            "any violation means the factor used information not available "
            "at the factor date"
        ),
    }


def audit_future_availability(
    fundamentals: pd.DataFrame,
    as_of: str | pd.Timestamp,
) -> dict[str, Any]:
    """Detect fundamentals rows whose availability date is after as-of."""
    if not isinstance(fundamentals, pd.DataFrame) or fundamentals.empty:
        raise ResearchInputError("fundamentals must be a non-empty DataFrame")
    if "date" not in fundamentals.columns:
        raise ResearchInputError("fundamentals must contain a date column")
    reference = pd.Timestamp(as_of)
    dates = pd.to_datetime(fundamentals["date"], errors="coerce")
    future = dates > reference
    count = int(future.sum())
    return {
        "audit": "future_availability",
        "as_of": reference.isoformat(),
        "future_rows": count,
        "future_dates": sorted(
            {d.date().isoformat() for d in dates[future].dt.normalize().unique()}
        ),
        "clean": count == 0,
        "note": (
            "future-availability rows must be dropped before any backtest "
            "that ends before their availability date"
        ),
    }


def audit_rank_mask_order(
    signals: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    reference: pd.DataFrame | None = None,
    selection_dates: Sequence[pd.Timestamp] | None = None,
) -> dict[str, Any]:
    """Verify mask-before-rank on the selection panel.

    Two checks:

    1. **missing-mask check** — no positive signal may exist for a symbol
       that was not a member on a *selection date*. Any violation means
       the code forgot the membership mask entirely.
    2. **order check** (when ``reference`` is supplied) — the selection
       must equal a mask-before-rank reference selection. Ranking all
       symbols first and masking afterwards yields *different* winners
       (non-members steal ranks), so a rank-then-mask implementation is
       detected even though its final panel contains only members.

    ``selection_dates`` restricts the audit to the dates on which the
    portfolio actually chooses (e.g. monthly rebalance dates). Between
    rebalances the engine legitimately holds previously chosen positions
    (a mid-month delisting is not knowable at the prior month-end), so a
    daily check would report realistic holdings as violations. Default:
    every date (strict).

    ``reference`` should be the same strategy's selection computed with
    membership applied before ranking.
    """
    if not signals.index.equals(membership.index) or not signals.columns.equals(
        membership.columns
    ):
        # Align defensively; absent cells are False (conservative).
        # fillna BEFORE astype(bool): NaN.astype(bool) is True.
        aligned = (
            membership.reindex(index=signals.index, columns=signals.columns)
            .fillna(False)
            .astype(bool)
        )
    else:
        aligned = membership.astype(bool)
    if selection_dates is not None:
        dates = [pd.Timestamp(value) for value in selection_dates]
        position_mask = signals.index.isin(dates)
    else:
        position_mask = np.ones(len(signals), dtype=bool)
    selected = signals.fillna(0.0) > 0
    missing_mask = selected & ~aligned
    missing_count = int(missing_mask.to_numpy()[position_mask].sum())
    examples: list[dict[str, Any]] = []
    if missing_count:
        rows = np.where(position_mask)[0]
        dates_, symbols_ = np.where(missing_mask.to_numpy())
        seen = 0
        for date_index, symbol_index in zip(dates_, symbols_):
            if date_index not in rows:
                continue
            examples.append(
                {
                    "date": signals.index[date_index].isoformat(),
                    "symbol": str(signals.columns[symbol_index]),
                }
            )
            seen += 1
            if seen >= 5:
                break

    order_violations: list[dict[str, Any]] = []
    if reference is not None:
        if not reference.index.equals(signals.index) or not reference.columns.equals(
            signals.columns
        ):
            raise ResearchInputError(
                "reference selection must align with the signals panel"
            )
        difference = (signals.fillna(0.0) - reference.fillna(0.0)).abs()
        differing = difference > 1e-9
        rows = np.where(position_mask)[0]
        dates_, symbols_ = np.where(differing.to_numpy())
        seen = 0
        for date_index, symbol_index in zip(dates_, symbols_):
            if date_index not in rows:
                continue
            order_violations.append(
                {
                    "date": signals.index[date_index].isoformat(),
                    "symbol": str(signals.columns[symbol_index]),
                    "observed": float(signals.iloc[date_index, symbol_index]),
                    "expected": float(reference.iloc[date_index, symbol_index]),
                }
            )
            seen += 1
            if seen >= 5:
                break
    return {
        "audit": "rank_mask_order",
        "violations": missing_count,
        "order_violations": order_violations,
        "examples": examples,
        "checked_dates": int(position_mask.sum()),
        "clean": missing_count == 0 and not order_violations,
        "note": (
            "violations = selections of non-members (missing mask); "
            "order_violations = selections that differ from the "
            "mask-before-rank reference (rank-then-mask bug)"
        ),
    }


def audit_survivorship(
    membership: pd.DataFrame,
    prices: pd.DataFrame,
) -> dict[str, Any]:
    """Inspect the membership panel for survivorship anomalies.

    Flags:

    * ``priced_but_never_eligible`` — symbols with price data that are
      never index members; they are invisible to PIT research, which is
      conservative but worth knowing (a hidden universe).
    * ``membership_before_prices`` — membership claims eligibility before
      the first price observation (data gap).
    * ``delisted_but_still_priced`` — symbols that lose membership while
      their price series continues; expected in PIT panels (the price
      history is raw), recorded as information.
    """
    if not membership.index.equals(prices.index):
        raise ResearchInputError("membership and prices must share an index")
    member_ever = membership.astype(bool).any(axis=0)
    priced = prices.notna().any(axis=0)
    never_eligible = [
        str(s) for s in prices.columns if priced[s] and not member_ever[s]
    ]

    first_price = prices.apply(lambda col: col.first_valid_index())
    first_membership = membership.astype(bool).apply(
        lambda col: col.index[col][0] if col.any() else None
    )
    membership_before_prices = []
    for symbol in prices.columns:
        fp = first_price[symbol]
        fm = first_membership[symbol]
        if fp is not None and fm is not None and fm < fp:
            membership_before_prices.append(str(symbol))

    last_membership = membership.astype(bool).apply(
        lambda col: col.index[col][-1] if col.any() else None
    )
    delisted = []
    for symbol in prices.columns:
        lm = last_membership[symbol]
        if lm is None:
            continue
        last_price = prices[symbol].last_valid_index()
        if last_price is not None and last_price > lm:
            delisted.append(str(symbol))
    return {
        "audit": "survivorship",
        "priced_but_never_eligible": never_eligible,
        "membership_before_prices": membership_before_prices,
        "delisted_but_still_priced": delisted,
        "clean": not never_eligible and not membership_before_prices,
        "note": (
            "delisted-but-still-priced is expected for raw price panels "
            "with point-in-time membership; the other two flags are data "
            "integrity issues"
        ),
    }


def audit_holdout_isolation(
    dev_window: tuple[str, str],
    holdout_window: tuple[str, str],
) -> dict[str, Any]:
    """Verify the development and holdout windows are disjoint in time."""
    dev_start, dev_end = (pd.Timestamp(value) for value in dev_window)
    holdout_start, holdout_end = (pd.Timestamp(value) for value in holdout_window)
    overlap = dev_start <= holdout_end and holdout_start <= dev_end
    gap = (holdout_start - dev_end).days if holdout_start > dev_end else 0
    return {
        "audit": "holdout_isolation",
        "dev_window": [dev_start.isoformat(), dev_end.isoformat()],
        "holdout_window": [holdout_start.isoformat(), holdout_end.isoformat()],
        "overlapping": bool(overlap),
        "gap_days": int(gap),
        "clean": not overlap,
        "note": (
            "development and holdout must be strictly disjoint; overlap is "
            "holdout leakage"
        ),
    }


@dataclass(frozen=True, slots=True)
class LeakageAudit:
    """Bundle of audit results with an aggregate verdict."""

    audits: Mapping[str, Any]

    def clean(self) -> bool:
        return all(audit.get("clean", True) for audit in self.audits.values())

    def findings(self) -> dict[str, Any]:
        return {
            name: audit
            for name, audit in self.audits.items()
            if not audit.get("clean", True)
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean(),
            "audits": dict(self.audits),
            "findings": self.findings(),
        }
