"""Phase 2 tests for fundamental quality factors."""

from __future__ import annotations

import pandas as pd
import pytest

from models.quality import (
    CompositeQualityFactor,
    DebtQualityFactor,
    QualityFactor,
    RoeQualityFactor,
)
from research.contracts import FactorMetadata, ResearchInputError


def _fundamentals() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-06-30",
                    "2026-06-30",
                    "2026-06-30",
                    "2026-09-30",
                    "2026-09-30",
                    "2026-09-30",
                ]
            ),
            "symbol": ["A", "B", "C", "A", "B", "C"],
            "roe": [0.20, 0.05, 0.15, 0.18, 0.09, 0.25],
            "debt_to_equity": [1.5, 0.2, 0.8, 1.2, 0.3, 0.9],
        }
    )


class TestRoeQuality:
    def test_rank_panel(self) -> None:
        panel = RoeQualityFactor().compute(_fundamentals())
        assert panel.shape == (2, 3)
        # A has the best ROE on both dates.
        assert panel.iloc[0]["A"] == 1.0
        assert panel.iloc[1]["C"] == 1.0
        assert panel.iloc[0]["B"] == 1.0 / 3.0

    def test_metadata(self) -> None:
        metadata = RoeQualityFactor().metadata
        assert isinstance(metadata, FactorMetadata)
        assert metadata.family == "quality"

    def test_missing_metric_rejected(self) -> None:
        fundamentals = _fundamentals().drop(columns=["roe"])
        with pytest.raises(ResearchInputError, match="roe"):
            RoeQualityFactor().compute(fundamentals)

    def test_empty_fundamentals_rejected(self) -> None:
        with pytest.raises(ResearchInputError):
            RoeQualityFactor().compute(pd.DataFrame())


class TestDebtQuality:
    def test_low_debt_ranks_higher(self) -> None:
        panel = DebtQualityFactor().compute(_fundamentals())
        # B has the lowest leverage on both dates.
        assert panel.iloc[0]["B"] == 1.0
        assert panel.iloc[1]["B"] == 1.0

    def test_missing_leverage_stays_nan(self) -> None:
        fundamentals = _fundamentals().copy()
        fundamentals.loc[1, "debt_to_equity"] = float("nan")
        panel = DebtQualityFactor().compute(fundamentals)
        assert pd.isna(panel.iloc[0]["B"])


class TestComposite:
    def test_composite_weights_and_fills(self) -> None:
        composite = CompositeQualityFactor(
            [RoeQualityFactor(), DebtQualityFactor()], weights=[0.7, 0.3]
        )
        panel = composite.compute(_fundamentals())
        assert panel.shape == (2, 3)
        # All ranks exist here; check the weighted sum exactly.
        roe = RoeQualityFactor().compute(_fundamentals())
        debt = DebtQualityFactor().compute(_fundamentals())
        expected = 0.7 * roe + 0.3 * debt
        assert panel.equals(expected)

    def test_composite_fills_missing_with_median(self) -> None:
        fundamentals = _fundamentals().copy()
        fundamentals.loc[0, "debt_to_equity"] = float("nan")  # A missing leverage
        composite = CompositeQualityFactor([RoeQualityFactor(), DebtQualityFactor()])
        panel = composite.compute(fundamentals)
        assert not panel.isna().any().any()
        roe = RoeQualityFactor().compute(_fundamentals())
        debt = DebtQualityFactor().compute(fundamentals)
        debt_filled = debt.reindex(panel.index, columns=panel.columns)
        medians = debt_filled.median(axis=1)
        debt_filled = debt_filled.fillna(medians).fillna(0.5)
        expected = 0.5 * roe + 0.5 * debt_filled
        assert panel.equals(expected)

    def test_weights_normalized(self) -> None:
        composite = CompositeQualityFactor(
            [RoeQualityFactor(), DebtQualityFactor()], weights=[3.0, 1.0]
        )
        assert composite.weights[0] == pytest.approx(0.75)
        assert composite.weights[1] == pytest.approx(0.25)

    def test_invalid_weights_rejected(self) -> None:
        with pytest.raises(ResearchInputError):
            CompositeQualityFactor([RoeQualityFactor()], weights=[-1.0])
        with pytest.raises(ResearchInputError):
            CompositeQualityFactor([RoeQualityFactor()], weights=[0.0])
        with pytest.raises(ResearchInputError):
            CompositeQualityFactor([])

    def test_is_quality_factor_interface_compatible(self) -> None:
        # QualityFactor is the documented interface for quality research.
        class _Adapter(QualityFactor):
            def __init__(self, inner) -> None:
                self._inner = inner

            @property
            def metadata(self) -> FactorMetadata:
                return self._inner.metadata

            def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
                return self._inner.compute(fundamentals)

        adapter = _Adapter(CompositeQualityFactor([RoeQualityFactor()]))
        assert adapter.compute(_fundamentals()).shape == (2, 3)


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        first = CompositeQualityFactor(
            [RoeQualityFactor(), DebtQualityFactor()]
        ).compute(_fundamentals())
        second = CompositeQualityFactor(
            [RoeQualityFactor(), DebtQualityFactor()]
        ).compute(_fundamentals())
        assert first.equals(second)
