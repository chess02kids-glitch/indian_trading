"""Research-only interface for fundamental quality factors."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from research.contracts import FactorMetadata, ResearchInputError


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
        """Validate and copy a non-empty fundamental input frame."""
        if not isinstance(fundamentals, pd.DataFrame) or fundamentals.empty:
            raise ResearchInputError("fundamentals must be a non-empty pandas DataFrame")
        if not isinstance(fundamentals.index, pd.DatetimeIndex):
            raise ResearchInputError("fundamentals must use a DatetimeIndex")
        return fundamentals.copy()
