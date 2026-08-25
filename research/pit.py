"""Point-in-time ranking helpers.

Correct order is always:

    mask ineligible assets → rank eligible assets

Ranking the full panel and then masking is a silent leakage / look-ahead
bug: delisted or not-yet-members change the cross-sectional distribution.
"""

from __future__ import annotations

import pandas as pd

from .contracts import ResearchInputError

__all__ = ["active_mask", "rank_eligible"]


def active_mask(
    index: pd.DatetimeIndex,
    columns: pd.Index,
    membership: pd.DataFrame | None,
) -> pd.DataFrame:
    """Align a boolean membership panel, defaulting missing cells to False."""
    if membership is None:
        return pd.DataFrame(True, index=index, columns=columns)
    if not isinstance(membership, pd.DataFrame):
        raise ResearchInputError("membership must be a DataFrame or None")
    return membership.reindex(index=index, columns=columns).astype(bool).fillna(False)


def rank_eligible(
    values: pd.DataFrame,
    membership: pd.DataFrame | None,
    *,
    ascending: bool = True,
    method: str = "first",
) -> pd.DataFrame:
    """Percentile-rank *only* assets that are members on that date.

    Non-members become NaN *before* ranking so they cannot occupy a rank
    slot. Callers then apply a quantile threshold on the eligible ranks.
    """
    if not isinstance(values, pd.DataFrame) or values.empty:
        raise ResearchInputError("values must be a non-empty DataFrame")
    mask = active_mask(values.index, values.columns, membership)
    eligible = values.where(mask)
    return eligible.rank(axis=1, pct=True, method=method, ascending=ascending)
