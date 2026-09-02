"""Tests for daily/weekly/monthly portfolio reporting."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engine import (
    MEMBERSHIP_FROM_PRICES,
    BacktestConfig,
    VectorBTResearchEngine,
)
from research.contracts import CostModel, ResearchInputError
from research.reporting import generate_periodic_reports


def _result(periods: int = 250):
    index = pd.date_range("2023-01-02", periods=periods, freq="B")
    prices = pd.DataFrame(
        {
            "A": 100
            * np.exp(np.cumsum(np.random.default_rng(1).normal(0.0005, 0.01, periods))),
            "B": 100
            * np.exp(np.cumsum(np.random.default_rng(2).normal(0.0003, 0.01, periods))),
        },
        index=index,
    )
    weights = pd.DataFrame(0.5, index=index, columns=["A", "B"])
    return VectorBTResearchEngine(
        BacktestConfig(use_vectorbt=False, cost_model=CostModel(1, 1))
    ).run(prices, weights, strategy_name="periodic", universe_history=MEMBERSHIP_FROM_PRICES)


def test_periodic_reports_cover_daily_weekly_monthly() -> None:
    reports = generate_periodic_reports(_result(), periods=("D", "W", "M"))
    assert set(reports) == {"D", "W", "M"}
    assert reports["M"].label == "monthly"
    assert len(reports["M"].periods) > 0


def test_period_rows_have_required_fields() -> None:
    result = _result()
    month = generate_periodic_reports(result, periods=("M",))["M"].periods[0]
    for field in (
        "period_start",
        "period_end",
        "period_return",
        "cumulative_return",
        "volatility",
        "sharpe",
        "max_drawdown",
        "exposure",
        "max_holding",
        "num_holdings",
        "turnover",
    ):
        assert field in month
    assert month["exposure"] == pytest.approx(1.0, abs=1e-9)
    assert month["num_holdings"] == 2


def test_periodic_reports_include_factor_exposure() -> None:
    result = _result()
    factor = pd.DataFrame(
        0.5, index=result.returns.index, columns=result.weights.columns
    )
    month = generate_periodic_reports(result, periods=("M",), factor_values=factor)[
        "M"
    ].periods[0]
    assert "factor_exposure" in month
    assert month["factor_exposure"] == pytest.approx(0.5)


def test_periodic_reports_reject_bad_periods() -> None:
    with pytest.raises(ResearchInputError, match="unsupported report periods"):
        generate_periodic_reports(_result(), periods=("Y",))


def test_periodic_reports_are_deterministic() -> None:
    first = generate_periodic_reports(_result(), periods=("M",))["M"].to_dict()
    second = generate_periodic_reports(_result(), periods=("M",))["M"].to_dict()
    assert first == second
