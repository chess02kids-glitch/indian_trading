"""Phase 2 tests for the configurable India cost model and its engine use."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from config.costs import (
    IndiaChargeTable,
    load_charge_table,
)
from research.contracts import CostModel


def _prices(periods: int = 60) -> pd.DataFrame:
    index = pd.date_range("2025-01-02", periods=periods, freq="B")
    return pd.DataFrame(
        {"A": 100 * 1.001 ** pd.Series(range(periods), index=index).values},
        index=index,
    )


class TestChargeTable:
    def test_defaults_are_configurable(self) -> None:
        table = load_charge_table(brokerage_bps=0.0, stt_buy_bps=12.0)
        assert table.brokerage_bps == 0.0
        assert table.stt_buy_bps == 12.0

    def test_environment_override(self, monkeypatch) -> None:
        monkeypatch.setenv("QUANT_COST_STT_SELL_BPS", "15")
        table = load_charge_table()
        assert table.stt_sell_bps == 15.0

    def test_environment_override_invalid(self) -> None:
        with pytest.raises(ValueError, match="numeric"):
            load_charge_table({"QUANT_COST_BROKERAGE_BPS": "oops"})

    def test_negative_charge_rejected(self) -> None:
        with pytest.raises(ValueError):
            IndiaChargeTable(stt_buy_bps=-1.0)

    def test_gst_applies_to_fee_components(self) -> None:
        table = IndiaChargeTable(gst_rate=0.18)
        expected_gst = (
            table.brokerage_bps + table.exchange_buy_bps + table.sebi_fee_bps
        ) * 0.18
        assert table.buy_bps == pytest.approx(
            table.brokerage_bps
            + table.stt_buy_bps
            + table.exchange_buy_bps
            + table.sebi_fee_bps
            + table.stamp_duty_buy_bps
            + expected_gst
        )

    def test_table_version_recorded(self) -> None:
        assert "verify" in load_charge_table().table_version.lower()


class TestScenarios:
    def test_scenario_rates_differ(self) -> None:
        optimistic = IndiaCostModel(scenario="optimistic")
        base = IndiaCostModel(scenario="base")
        pessimistic = IndiaCostModel(scenario="pessimistic")
        assert (
            optimistic.market_cost_bps
            < base.market_cost_bps
            < pessimistic.market_cost_bps
        )

    def test_regulatory_costs_same_across_scenarios(self) -> None:
        table = load_charge_table()
        for scenario in ("optimistic", "base", "pessimistic"):
            model = IndiaCostModel(table=table, scenario=scenario)
            assert model.transaction_cost_bps == pytest.approx(
                (table.buy_bps + table.sell_bps) / 2.0
            )

    def test_unknown_scenario_rejected(self) -> None:
        with pytest.raises(ValueError, match="scenario"):
            IndiaCostModel(scenario="moonshot")

    def test_cost_breakdown(self) -> None:
        model = IndiaCostModel(scenario="base")
        breakdown = model.cost_breakdown(buy_value=100_000.0, sell_value=80_000.0)
        assert breakdown["total"] > 0
        assert breakdown["buy"]["subtotal"] > 0
        assert breakdown["sell"]["subtotal"] > 0
        assert breakdown["scenario"] == "base"
        for charge in ("brokerage", "stt", "exchange", "sebi", "stamp_duty", "gst"):
            assert breakdown["buy"][charge] >= 0

    def test_zero_traded_value_costs_nothing(self) -> None:
        breakdown = IndiaCostModel().cost_breakdown(0.0, 0.0)
        assert breakdown["total"] == 0.0

    def test_negative_value_rejected(self) -> None:
        with pytest.raises(ValueError):
            IndiaCostModel().cost_breakdown(-1.0, 0.0)


class TestEngineIntegration:
    def test_india_cost_model_drop_in(self) -> None:
        prices = _prices()
        weights = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
        engine = VectorBTResearchEngine(
            BacktestConfig(
                use_vectorbt=False,
                cost_model=IndiaCostModel(scenario="pessimistic"),
            )
        )
        result = engine.run(prices, weights, strategy_name="costs")
        # Pessimistic market cost must be visible in metadata.
        assert result.metadata["cost_model"]["scenario"] == "pessimistic"
        assert result.metrics.cost_drag >= 0
        assert result.metrics.trade_count >= 1
        assert result.metrics.sortino >= 0
        assert result.metrics.win_rate is not None

    def test_cost_scenario_changes_cost_drag(self) -> None:
        prices = _prices()
        weights = pd.DataFrame(0.5, index=prices.index, columns=prices.columns)
        weights = weights * 1.0
        results = {}
        for scenario in ("optimistic", "pessimistic"):
            engine = VectorBTResearchEngine(
                BacktestConfig(
                    use_vectorbt=False,
                    cost_model=IndiaCostModel(scenario=scenario),
                )
            )
            results[scenario] = engine.run(prices, weights, strategy_name=scenario)
        assert (
            results["pessimistic"].metrics.cost_drag
            > results["optimistic"].metrics.cost_drag
        )
        assert (
            results["pessimistic"].metrics.total_return
            < results["optimistic"].metrics.total_return
        )

    def test_legacy_cost_model_still_works(self) -> None:
        prices = _prices()
        weights = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
        engine = VectorBTResearchEngine(
            BacktestConfig(use_vectorbt=False, cost_model=CostModel(5, 2))
        )
        result = engine.run(prices, weights, strategy_name="legacy")
        assert result.metrics.cost_drag >= 0

    def test_cost_model_serializable_in_metadata(self) -> None:
        prices = _prices(30)
        weights = pd.DataFrame(1.0, index=prices.index, columns=prices.columns)
        engine = VectorBTResearchEngine(
            BacktestConfig(use_vectorbt=False, cost_model=IndiaCostModel())
        )
        result = engine.run(prices, weights, strategy_name="meta")
        payload = result.to_dict()
        assert payload["metadata"]["cost_model"]["model"] == "india_cost_model"
