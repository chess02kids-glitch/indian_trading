"""India-specific transaction cost model for backtesting and paper fills.

``IndiaCostModel`` composes the configurable charge table
(:mod:`config.costs`) with a market-condition scenario (optimistic/base/
pessimistic). It exposes the same ``transaction_cost_bps`` /
``slippage_bps`` interface as :class:`research.contracts.CostModel`, so it
is a drop-in for the backtest engine, and it adds a detailed per-charge
breakdown for reports.

Nothing here is hardcoded truth: every regulatory rate comes from
``config.costs.load_charge_table`` (environment-overridable) and every
market-dependent rate comes from the chosen scenario.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from config.costs import (
    SCENARIO_MARKET_CONDITIONS,
    CostScenario,
    IndiaChargeTable,
    load_charge_table,
)

__all__ = ["IndiaCostModel"]


def _bps_to_rate(bps: float) -> float:
    return bps / 10_000.0


@dataclass(frozen=True)
class IndiaCostModel:
    """Round-trip cost model with per-side regulatory and market components."""

    table: IndiaChargeTable | None = None
    scenario: str = CostScenario.BASE
    spread_bps: float | None = None
    slippage_bps: float | None = None

    def __post_init__(self) -> None:
        table = self.table or load_charge_table()
        scenario = CostScenario.validate(self.scenario)
        conditions = SCENARIO_MARKET_CONDITIONS[scenario]
        spread = (
            float(self.spread_bps)
            if self.spread_bps is not None
            else float(conditions["spread_bps"])
        )
        slippage = (
            float(self.slippage_bps)
            if self.slippage_bps is not None
            else float(conditions["slippage_bps"])
        )
        for name, value in (("spread_bps", spread), ("slippage_bps", slippage)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        object.__setattr__(self, "table", table)
        object.__setattr__(self, "scenario", scenario)
        object.__setattr__(self, "spread_bps", spread)
        object.__setattr__(self, "slippage_bps", slippage)

    # -- engine interface (drop-in for research.contracts.CostModel) --------

    @property
    def transaction_cost_bps(self) -> float:
        """One-way average regulatory cost used by the backtest engine."""
        return (self.table.buy_bps + self.table.sell_bps) / 2.0

    @property
    def market_cost_bps(self) -> float:
        """Market-dependent cost (spread + slippage) for the scenario.

        The backtest engine uses this attribute when present, falling back
        to ``slippage_bps`` for the legacy ``research.contracts.CostModel``.
        """
        return float(self.spread_bps) + float(self.slippage_bps)

    @property
    def proportional_rate(self) -> float:
        """Combined one-way rate as a decimal (regulatory + market)."""
        return (self.transaction_cost_bps + self.market_cost_bps) / 10_000.0

    # -- detailed breakdown --------------------------------------------------

    def cost_breakdown(self, buy_value: float, sell_value: float) -> dict[str, float]:
        """Per-charge cost in currency units for given traded values.

        ``buy_value``/``sell_value`` are traded notional on each side (e.g.
        from a rebalance's turnover split).
        """
        if buy_value < 0 or sell_value < 0:
            raise ValueError("buy_value and sell_value must be non-negative")
        table = self.table

        def side_cost(
            value: float, stt: float, exchange: float, stamp: float
        ) -> dict[str, float]:
            if value <= 0:
                return {
                    "brokerage": 0.0,
                    "stt": 0.0,
                    "exchange": 0.0,
                    "sebi": 0.0,
                    "stamp_duty": 0.0,
                    "gst": 0.0,
                    "subtotal": 0.0,
                }
            brokerage = value * _bps_to_rate(table.brokerage_bps)
            stt_cost = value * _bps_to_rate(stt)
            exchange_cost = value * _bps_to_rate(exchange)
            sebi = value * _bps_to_rate(table.sebi_fee_bps)
            stamp = value * _bps_to_rate(stamp)
            gst = (brokerage + exchange_cost + sebi) * table.gst_rate
            subtotal = brokerage + stt_cost + exchange_cost + sebi + stamp + gst
            return {
                "brokerage": brokerage,
                "stt": stt_cost,
                "exchange": exchange_cost,
                "sebi": sebi,
                "stamp_duty": stamp,
                "gst": gst,
                "subtotal": subtotal,
            }

        buy = side_cost(
            buy_value,
            table.stt_buy_bps,
            table.exchange_buy_bps,
            table.stamp_duty_buy_bps,
        )
        sell = side_cost(
            sell_value,
            table.stt_sell_bps,
            table.exchange_sell_bps,
            table.stamp_duty_sell_bps,
        )
        market_cost = (buy_value + sell_value) * _bps_to_rate(self.market_cost_bps)
        total = buy["subtotal"] + sell["subtotal"] + market_cost
        return {
            "scenario": self.scenario,
            "table_version": table.table_version,
            "buy": buy,
            "sell": sell,
            "spread_and_slippage": market_cost,
            "total": total,
        }

    def to_dict(self) -> dict[str, float | str]:
        return {
            "model": "india_cost_model",
            "scenario": self.scenario,
            "spread_bps": float(self.spread_bps),
            "slippage_bps": float(self.slippage_bps),
            "table": self.table.to_dict(),
        }
