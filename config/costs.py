"""Configurable transaction-charge assumptions for Indian cash equity.

These values are *configurable defaults*, not permanent truth: STT, SEBI,
exchange, and stamp-duty rates change with regulatory revisions. Every value
can be overridden per environment (``QUANT_COST_*`` variables) or per
backtest via :class:`backtest.costs.IndiaCostModel`. Before any production
use, verify the table against the current regulatory schedule and bump
``TABLE_VERSION``.

All rates are basis points (bps) of traded value, per side, unless noted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "TABLE_VERSION",
    "CostScenario",
    "IndiaChargeTable",
    "SCENARIO_MARKET_CONDITIONS",
    "load_charge_table",
]

TABLE_VERSION = "india-charges-2026.08 (verify before production)"

#: Market-condition scenarios. Only market-dependent costs (spread, slippage)
#: vary by scenario; regulatory charges are the same in every scenario.
SCENARIO_MARKET_CONDITIONS: Mapping[str, Mapping[str, float]] = {
    "optimistic": {"spread_bps": 1.0, "slippage_bps": 2.0},
    "base": {"spread_bps": 2.0, "slippage_bps": 5.0},
    "pessimistic": {"spread_bps": 5.0, "slippage_bps": 15.0},
}


class CostScenario(str):
    """Supported market-condition scenarios (optimistic/base/pessimistic)."""

    OPTIMISTIC = "optimistic"
    BASE = "base"
    PESSIMISTIC = "pessimistic"

    @classmethod
    def validate(cls, name: str) -> str:
        normalized = str(name).strip().lower()
        if normalized not in SCENARIO_MARKET_CONDITIONS:
            raise ValueError(
                f"unknown cost scenario {name!r}; expected one of "
                f"{sorted(SCENARIO_MARKET_CONDITIONS)}"
            )
        return normalized


@dataclass(frozen=True)
class IndiaChargeTable:
    """Per-side charge schedule for delivery (CNC) cash equity.

    ``gst_bps`` is computed as 18% of (brokerage + exchange + SEBI) because
    GST applies to those fee components, not to traded value directly.
    """

    table_version: str = TABLE_VERSION
    brokerage_bps: float = 5.0
    stt_buy_bps: float = 10.0
    stt_sell_bps: float = 10.0
    exchange_buy_bps: float = 4.0
    exchange_sell_bps: float = 4.0
    sebi_fee_bps: float = 0.01
    stamp_duty_buy_bps: float = 2.0
    stamp_duty_sell_bps: float = 0.0
    gst_rate: float = 0.18

    def __post_init__(self) -> None:
        fields = vars(self)
        for name, value in fields.items():
            if name in ("table_version", "gst_rate"):
                continue
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"charge {name} must be a non-negative number")
        if not 0 <= self.gst_rate <= 1:
            raise ValueError("gst_rate must be in [0, 1]")

    def _gst_bps(self) -> float:
        base = self.brokerage_bps + self.exchange_buy_bps + self.sebi_fee_bps
        return base * self.gst_rate

    @property
    def buy_bps(self) -> float:
        """Total buy-side cost in bps (regulatory + GST, excl. spread/slippage)."""
        return (
            self.brokerage_bps
            + self.stt_buy_bps
            + self.exchange_buy_bps
            + self.sebi_fee_bps
            + self.stamp_duty_buy_bps
            + self._gst_bps()
        )

    @property
    def sell_bps(self) -> float:
        """Total sell-side cost in bps (regulatory + GST, excl. spread/slippage)."""
        return (
            self.brokerage_bps
            + self.stt_sell_bps
            + self.exchange_sell_bps
            + self.sebi_fee_bps
            + self.stamp_duty_sell_bps
            + self._gst_bps()
        )

    def to_dict(self) -> dict[str, float | str]:
        return {
            "table_version": self.table_version,
            "brokerage_bps": self.brokerage_bps,
            "stt_buy_bps": self.stt_buy_bps,
            "stt_sell_bps": self.stt_sell_bps,
            "exchange_buy_bps": self.exchange_buy_bps,
            "exchange_sell_bps": self.exchange_sell_bps,
            "sebi_fee_bps": self.sebi_fee_bps,
            "stamp_duty_buy_bps": self.stamp_duty_buy_bps,
            "stamp_duty_sell_bps": self.stamp_duty_sell_bps,
            "gst_rate": self.gst_rate,
            "buy_bps": self.buy_bps,
            "sell_bps": self.sell_bps,
        }


_ENV_KEYS = (
    "brokerage_bps",
    "stt_buy_bps",
    "stt_sell_bps",
    "exchange_buy_bps",
    "exchange_sell_bps",
    "sebi_fee_bps",
    "stamp_duty_buy_bps",
    "stamp_duty_sell_bps",
    "gst_rate",
)


def load_charge_table(
    environ: Mapping[str, str] | None = None,
    **overrides: float,
) -> IndiaChargeTable:
    """Build a charge table from defaults + environment + explicit overrides.

    Environment keys are ``QUANT_COST_BROKERAGE_BPS`` etc. Explicit
    ``overrides`` win over the environment.
    """
    environment = os.environ if environ is None else environ
    values: dict[str, float] = {}
    for key in _ENV_KEYS:
        raw = environment.get(f"QUANT_COST_{key.upper()}")
        if raw is not None:
            try:
                values[key] = float(raw)
            except ValueError as exc:
                raise ValueError(f"QUANT_COST_{key.upper()} must be numeric") from exc
    values.update(overrides)
    return IndiaChargeTable(**values)
