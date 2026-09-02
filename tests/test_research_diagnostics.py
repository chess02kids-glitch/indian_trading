"""Tests for deterministic factor diagnostics."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.engine import (
    MEMBERSHIP_FROM_PRICES,
    BacktestConfig,
    VectorBTResearchEngine,
)
from research.contracts import (
    CostModel,
    MarketData,
    ResearchInputError,
    Signal,
    Strategy,
)
from research.diagnostics import (
    FactorDiagnostics,
    factor_contribution_breakdown,
    factor_decay,
    rank_stability,
    sector_exposure,
    turnover_attribution,
    volatility_contribution,
)


def _panel(periods: int = 120) -> pd.DataFrame:
    index = pd.date_range("2023-01-02", periods=periods, freq="B")
    rng = np.random.default_rng(11)
    # A-dominant market: A drifts, B/C/D flat, E/F laggards.
    drifts = [0.0015, 0.0002, 0.0, -0.0002, -0.0005, -0.0008]
    returns = np.column_stack(
        [drift + rng.normal(0, 0.008, periods) for drift in drifts]
    )
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(close, index=index, columns=[f"S{i}" for i in range(6)])


class TestFactorDecay:
    def test_ic_profiles_are_deterministic_and_bounded(self) -> None:
        close = _panel()
        returns = close.pct_change().fillna(0.0)
        # Factor: prior 5-day return (trailing momentum).
        momentum = close.pct_change(5)
        first = factor_decay({"momentum": momentum}, returns)
        second = factor_decay({"momentum": momentum}, returns)
        assert first == second
        assert set(first) == {"momentum"}
        assert set(first["momentum"]) == {"1", "5", "21", "63"}
        for value in first["momentum"].values():
            assert -1.0 <= value <= 1.0

    def test_inverted_factor_has_opposite_ic(self) -> None:
        close = _panel()
        returns = close.pct_change().fillna(0.0)
        factor = close.pct_change(5)
        positive = factor_decay({"f": factor}, returns, horizons=(5,))
        negative = factor_decay({"f": -factor}, returns, horizons=(5,))
        assert negative["f"]["5"] == pytest.approx(-positive["f"]["5"], abs=1e-9)

    def test_decay_is_measurable(self) -> None:
        """Momentum IC at 1-day horizons is higher than at 63-day horizons."""
        close = _panel()
        returns = close.pct_change().fillna(0.0)
        factor = close.pct_change(5)
        profile = factor_decay({"momentum": factor}, returns)
        assert profile["momentum"]["1"] >= profile["momentum"]["63"] - 1e-9

    def test_rejects_misaligned_panels(self) -> None:
        close = _panel()
        returns = close.pct_change().fillna(0.0)
        with pytest.raises(ResearchInputError):
            factor_decay({"f": close.iloc[:10]}, returns)
        with pytest.raises(ResearchInputError):
            factor_decay({}, returns)


class TestRankStability:
    def test_constant_rank_is_perfectly_stable(self) -> None:
        index = pd.date_range("2024-01-01", periods=4, freq="B")
        panel = pd.DataFrame(
            np.tile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], (4, 1)),
            index=index,
            columns=list("ABCDEF"),
        )
        assert rank_stability({"f": panel})["f"] == pytest.approx(1.0)

    def test_reversed_rank_is_negatively_stable(self) -> None:
        index = pd.date_range("2024-01-01", periods=4, freq="B")
        panel = pd.DataFrame(
            [
                [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                [6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
                [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
                [6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
            ],
            index=index,
            columns=list("ABCDEF"),
        )
        assert rank_stability({"f": panel})["f"] == pytest.approx(-1.0)

    def test_uses_provided_rebalance_dates(self) -> None:
        index = pd.date_range("2024-01-01", periods=5, freq="B")
        panel = pd.DataFrame(
            np.tile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], (5, 1)),
            index=index,
            columns=list("ABCDEF"),
        )
        dates = [index[0], index[3]]
        assert rank_stability({"f": panel}, rebalance_dates=dates)["f"] == 1.0
        with pytest.raises(ResearchInputError):
            rank_stability(
                {"f": panel},
                rebalance_dates=[index[0], pd.Timestamp("2030-01-01")],
            )


class TestSectorExposure:
    def test_exposure_by_sector(self) -> None:
        index = pd.date_range("2024-01-01", periods=3, freq="B")
        weights = pd.DataFrame(
            [[0.5, 0.3, 0.2], [0.4, 0.4, 0.2], [0.6, 0.2, 0.2]],
            index=index,
            columns=["A", "B", "C"],
        )
        exposure = sector_exposure(weights, {"A": "tech", "B": "energy", "C": "energy"})
        assert exposure["energy"]["average_weight"] == pytest.approx(0.5)
        assert exposure["tech"]["average_weight"] == pytest.approx(0.5)
        assert exposure["energy"]["num_holdings"] == 2.0

    def test_missing_symbols_rejected(self) -> None:
        index = pd.date_range("2024-01-01", periods=3, freq="B")
        weights = pd.DataFrame(np.zeros((3, 2)), index=index, columns=["A", "B"])
        with pytest.raises(ResearchInputError):
            sector_exposure(weights, {"A": "tech"})


class TestTurnoverAttribution:
    def test_shares_sum_to_one(self) -> None:
        result = _engine_result()
        attribution = turnover_attribution(result)
        assert attribution["total_turnover"] > 0
        shares = sum(entry["share"] for entry in attribution["by_symbol"].values())
        assert shares == pytest.approx(1.0, abs=1e-9)
        sides = attribution["by_side"]
        assert sides["buys"] == pytest.approx(sides["sells"], abs=1e-9)
        assert sides["buys"] > 0

    def test_turnover_captures_abs_changes(self) -> None:
        result = _engine_result()
        attribution = turnover_attribution(result)
        changes = result.weights.diff().fillna(0.0).abs().sum().sum()
        assert attribution["total_turnover"] == pytest.approx(changes)


class TestVolatilityContribution:
    def test_equal_weight_uncorrelated_gives_equal_shares(self) -> None:
        index = pd.date_range("2023-01-02", periods=200, freq="B")
        rng = np.random.default_rng(3)
        returns = pd.DataFrame(
            rng.normal(0.0003, 0.01, size=(200, 2)),
            index=index,
            columns=["A", "B"],
        )
        weights = pd.DataFrame(0.5, index=index, columns=["A", "B"])
        contributions = volatility_contribution(weights, returns, window=40)
        total = sum(contributions.values())
        assert total == pytest.approx(1.0, abs=1e-6)
        assert contributions["A"] == pytest.approx(0.5, abs=0.08)
        assert contributions["B"] == pytest.approx(0.5, abs=0.08)

    def test_zero_weight_asset_contributes_nothing(self) -> None:
        index = pd.date_range("2023-01-02", periods=200, freq="B")
        rng = np.random.default_rng(4)
        returns = pd.DataFrame(
            rng.normal(0.0003, 0.01, size=(200, 2)),
            index=index,
            columns=["A", "B"],
        )
        weights = pd.DataFrame({"A": 1.0, "B": 0.0}, index=index, columns=["A", "B"])
        contributions = volatility_contribution(weights, returns, window=40)
        assert contributions["B"] == pytest.approx(0.0, abs=1e-9)
        assert contributions["A"] == pytest.approx(1.0, abs=1e-6)


class TestContributionBreakdown:
    def test_least_squares_attribution_recovers_factors(self) -> None:
        index = pd.date_range("2023-01-02", periods=300, freq="B")
        rng = np.random.default_rng(9)
        f1 = pd.Series(rng.normal(0.0005, 0.005, 300), index=index)
        f2 = pd.Series(rng.normal(0.0001, 0.005, 300), index=index)
        portfolio = pd.Series(
            0.6 * f1.to_numpy() + 0.4 * f2.to_numpy() + rng.normal(0, 0.0005, 300),
            index=index,
        )
        breakdown = factor_contribution_breakdown(portfolio, {"f1": f1, "f2": f2})
        assert breakdown["r_squared"] > 0.95
        assert breakdown["factors"]["f1"]["beta"] == pytest.approx(0.6, abs=0.05)
        assert breakdown["factors"]["f2"]["beta"] == pytest.approx(0.4, abs=0.05)
        assert abs(breakdown["residual"]) < 0.001

    def test_requires_alignment(self) -> None:
        index = pd.date_range("2023-01-02", periods=100, freq="B")
        series = pd.Series(0.001, index=index)
        disjoint = pd.Series(
            0.001, index=pd.date_range("2025-01-01", periods=100, freq="B")
        )
        with pytest.raises(ResearchInputError):
            factor_contribution_breakdown(series, {"f": disjoint})


class TestFactorDiagnosticsBundle:
    def test_to_dict_and_json_round_trip(self) -> None:
        close = _panel()
        returns = close.pct_change().fillna(0.0)
        result = _engine_result()
        diagnostics = FactorDiagnostics(
            factor_decay=factor_decay({"momentum": close.pct_change(5)}, returns),
            rank_stability=rank_stability({"momentum": close.pct_change(5)}),
            sector_exposure=sector_exposure(
                result.weights, {name: "sector" for name in result.weights.columns}
            ),
            turnover_attribution=turnover_attribution(result),
            volatility_contribution=volatility_contribution(
                pd.DataFrame(1.0 / 6, index=close.index, columns=close.columns),
                returns,
            ),
            contribution_breakdown={
                "factors": {"momentum": {"beta": 0.5, "contribution": 0.01}},
                "residual": 0.0,
                "r_squared": 0.5,
                "observations": 100,
            },
        )
        payload = diagnostics.to_dict()
        assert set(payload) == {
            "factor_decay",
            "rank_stability",
            "sector_exposure",
            "turnover_attribution",
            "volatility_contribution",
            "contribution_breakdown",
        }
        assert isinstance(diagnostics.to_json(), str)


class _ChurnStrategy(Strategy):
    """Strategy that toggles holdings between assets to create turnover."""

    @property
    def name(self) -> str:
        return "churn"

    def generate_signals(self, data: MarketData) -> Signal:
        values = pd.DataFrame(0.0, index=data.close.index, columns=data.close.columns)
        for position, date in enumerate(data.close.index):
            values.loc[date, data.close.columns[position % 2]] = 1.0
        return Signal(values)


def _engine_result():
    index = pd.date_range("2023-01-02", periods=60, freq="B")
    rng = np.random.default_rng(2)
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, (60, 4)), axis=0)),
        index=index,
        columns=["A", "B", "C", "D"],
    )
    engine = VectorBTResearchEngine(
        BacktestConfig(
            use_vectorbt=False,
            cost_model=CostModel(1, 1),
            rebalance_frequency="W",
        )
    )
    weights = _churn_weights(prices)
    return engine.run(prices, weights, strategy_name="churn", universe_history=MEMBERSHIP_FROM_PRICES)


def _churn_weights(prices: pd.DataFrame) -> pd.DataFrame:
    values = pd.DataFrame(0.0, index=prices.index, columns=prices.columns)
    for position, date in enumerate(prices.index):
        values.loc[date, prices.columns[position % 2]] = 1.0
    return values
