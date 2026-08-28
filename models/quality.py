"""Research-only interface for fundamental quality factors.

The fundamentals input is a long frame with at least ``date`` and ``symbol``
columns plus one column per fundamental metric. Factor outputs are
date x symbol panels aligned to the observed dates.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from math import isfinite

import pandas as pd

from research.contracts import Factor, FactorMetadata, ResearchInputError


class QualityFactor(ABC):
    """Interface for quality factors backed by fundamental observations.

    Fundamental data availability varies by source and point in time. This
    contract keeps quality research separate from technical factors while
    requiring the same metadata and deterministic panel output.
    """

    @property
    @abstractmethod
    def metadata(self) -> FactorMetadata:
        """Return factor metadata and parameter values."""

    @abstractmethod
    def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        """Return a date/symbol-aligned quality score panel."""

    @staticmethod
    def validate_fundamentals(fundamentals: pd.DataFrame) -> pd.DataFrame:
        """Validate and copy a non-empty fundamental input frame.

        Accepts either a DatetimeIndex or a long frame with a ``date``
        column; the returned frame always carries a DatetimeIndex plus a
        ``symbol`` column.
        """
        if not isinstance(fundamentals, pd.DataFrame) or fundamentals.empty:
            raise ResearchInputError(
                "fundamentals must be a non-empty pandas DataFrame"
            )
        frame = fundamentals.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            if "date" not in frame.columns:
                raise ResearchInputError(
                    "fundamentals must use a DatetimeIndex or a 'date' column"
                )
            frame["date"] = pd.to_datetime(frame["date"], errors="raise")
            frame = frame.set_index("date")
        return frame


def _pivot_long(fundamentals: pd.DataFrame, column: str) -> pd.DataFrame:
    """Pivot one fundamental metric into a date x symbol panel."""
    if column not in fundamentals.columns:
        raise ResearchInputError(f"fundamentals are missing column {column!r}")
    panel = fundamentals.pivot_table(
        index=fundamentals.index,
        columns="symbol",
        values=column,
        aggfunc="first",
    )
    panel = panel.astype(float)
    panel.columns = [str(symbol).strip().upper() for symbol in panel.columns]
    return panel


@dataclass(frozen=True)
class _FundamentalFactorBase(QualityFactor, ABC):
    """Shared validation/pivoting for fundamental quality factors."""

    metric: str = ""
    label: str | None = None

    def _panel(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        validated = self.validate_fundamentals(fundamentals)
        if "symbol" not in validated.columns:
            raise ResearchInputError("panel quality factors require a 'symbol' column")
        return _pivot_long(validated, self.metric)


@dataclass(frozen=True)
class RoeQualityFactor(_FundamentalFactorBase):
    """Cross-sectional rank of return on equity (higher is better)."""

    def __init__(self, label: str | None = None) -> None:
        object.__setattr__(self, "metric", "roe")
        object.__setattr__(self, "label", label)

    @property
    def metadata(self) -> FactorMetadata:
        return FactorMetadata(
            name=f"quality_roe_{self.label}" if self.label else "quality_roe",
            family="quality",
            description="Cross-sectional percentile rank of ROE.",
            parameters={"metric": "roe"},
        )

    def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        panel = self._panel(fundamentals)
        return panel.rank(axis=1, pct=True, method="first")


@dataclass(frozen=True)
class DebtQualityFactor(_FundamentalFactorBase):
    """Cross-sectional rank of debt-to-equity (lower is better)."""

    def __init__(self, label: str | None = None) -> None:
        object.__setattr__(self, "metric", "debt_to_equity")
        object.__setattr__(self, "label", label)

    @property
    def metadata(self) -> FactorMetadata:
        return FactorMetadata(
            name=(
                f"quality_low_debt_{self.label}" if self.label else "quality_low_debt"
            ),
            family="quality",
            description="Cross-sectional percentile rank of inverse debt/equity "
            "(lower leverage ranks higher).",
            parameters={"metric": "debt_to_equity"},
        )

    def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        panel = self._panel(fundamentals)
        return (-panel).rank(axis=1, pct=True, method="first").where(panel.notna())


@dataclass(frozen=True)
class PeRatioQualityFactor(_FundamentalFactorBase):
    """Cross-sectional rank of inverse P/E ratio (lower P/E ranks higher)."""

    def __init__(self, label: str | None = None) -> None:
        object.__setattr__(self, "metric", "pe_ratio")
        object.__setattr__(self, "label", label)

    @property
    def metadata(self) -> FactorMetadata:
        return FactorMetadata(
            name=f"value_pe_{self.label}" if self.label else "value_pe",
            family="value",
            description="Cross-sectional percentile rank of inverse P/E ratio (value tilt).",
            parameters={"metric": "pe_ratio"},
        )

    def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        panel = self._panel(fundamentals)
        return (-panel).rank(axis=1, pct=True, method="first").where(panel.notna())


class CompositeQualityFactor:
    """Weighted blend of fundamental quality factor panels.

    Panels are aligned to the union of observed dates/symbols. Missing
    values fall back to the cross-sectional median of that panel (0.5 for
    rank-based factors), so a stock missing one metric is not silently
    dropped from the composite.
    """

    def __init__(
        self,
        factors: list[Factor] | tuple[Factor, ...],
        weights: list[float] | tuple[float, ...] | None = None,
    ) -> None:
        if not factors:
            raise ResearchInputError("composite quality requires at least one factor")
        normalized = tuple(factors)
        if weights is None:
            weights = (1.0 / len(normalized),) * len(normalized)
        if len(weights) != len(normalized):
            raise ResearchInputError("weights must match the number of factors")
        if any(not isfinite(w) or w < 0 for w in weights):
            raise ResearchInputError("weights must be finite and non-negative")
        total = sum(weights)
        if total <= 0:
            raise ResearchInputError("weights must sum to a positive value")
        self.factors = normalized
        self.weights = tuple(w / total for w in weights)

    @property
    def metadata(self) -> FactorMetadata:
        return FactorMetadata(
            name="quality_composite",
            family="quality",
            description="Weighted blend of fundamental quality factors.",
            parameters={
                "factors": [factor.metadata.name for factor in self.factors],
                "weights": list(self.weights),
            },
        )

    def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        panels = [factor.compute(fundamentals) for factor in self.factors]
        for factor, panel in zip(self.factors, panels):
            if panel.empty:
                raise ResearchInputError(
                    f"quality factor {factor.metadata.name} produced no panel"
                )
        all_dates = sorted(set().union(*[set(panel.index) for panel in panels]))
        all_symbols = sorted(set().union(*[set(panel.columns) for panel in panels]))
        combined = None
        for panel, weight in zip(panels, self.weights):
            aligned = panel.reindex(index=all_dates, columns=all_symbols)
            medians = aligned.median(axis=1)
            filled = aligned.fillna(medians).fillna(0.5)
            scaled = filled * weight
            combined = (
                scaled if combined is None else combined.add(scaled, fill_value=0.0)
            )
        return combined
