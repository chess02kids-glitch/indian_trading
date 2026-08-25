"""Deterministic analytics for paper-trading records.

This module is a *read-only analytics layer*: it consumes plain-value fill
and position records (which may originate from paper execution) and produces
deterministic realized/unrealized PnL, slippage, exposure, turnover,
drawdown, and benchmark-divergence analytics. It never creates orders and
never imports execution, broker, or risk modules — paper execution remains
fully isolated from research.

All accounting is first-in-first-out per symbol and all mark-to-market uses
only supplied prices, so identical inputs always produce identical output
(deterministic reconciliation).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from research.contracts import ResearchInputError

__all__ = [
    "Fill",
    "PaperAnalytics",
    "PositionMark",
    "compute_paper_analytics",
]


@dataclass(frozen=True, slots=True)
class Fill:
    """One executed (or partially executed) paper fill.

    ``reference_price`` is the limit price the order was submitted at; the
    slippage of the fill is measured against it in basis points.
    """

    timestamp: pd.Timestamp
    symbol: str
    side: str  # BUY or SELL
    quantity: float
    price: float
    reference_price: float | None = None
    order_id: str = ""

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ResearchInputError("fill symbol must be non-empty")
        if self.side not in ("BUY", "SELL"):
            raise ResearchInputError(
                f"fill side must be BUY or SELL, got {self.side!r}"
            )
        for name, value in (
            ("quantity", self.quantity),
            ("price", self.price),
            ("reference_price", self.reference_price),
        ):
            if value is not None and (not np.isfinite(value) or value <= 0):
                raise ResearchInputError(f"{name} must be finite and positive")
        if not isinstance(self.timestamp, pd.Timestamp):
            raise ResearchInputError("fill timestamp must be a pandas Timestamp")


@dataclass(frozen=True, slots=True)
class PositionMark:
    """A mark-to-market position snapshot (used for unrealized PnL)."""

    timestamp: pd.Timestamp
    symbol: str
    quantity: float
    average_price: float

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ResearchInputError("position symbol must be non-empty")
        if not np.isfinite(self.quantity) or not np.isfinite(self.average_price):
            raise ResearchInputError("position quantity and price must be finite")
        if self.average_price <= 0:
            raise ResearchInputError("average_price must be positive")


@dataclass(frozen=True, slots=True)
class PaperAnalytics:
    """Deterministic aggregate analytics for one paper-trading period."""

    realized_pnl: dict[str, float]
    unrealized_pnl: dict[str, float]
    total_realized_pnl: float
    total_unrealized_pnl: float
    total_slippage: float
    average_slippage_bps: float
    peak_exposure: float
    average_exposure: float
    total_turnover: float
    total_turnover_cost: float
    max_drawdown: float
    benchmark_divergence: float
    equity_curve: Mapping[str, float]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible analytics mapping."""
        return {
            "realized_pnl": dict(self.realized_pnl),
            "unrealized_pnl": dict(self.unrealized_pnl),
            "total_realized_pnl": self.total_realized_pnl,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "total_slippage": self.total_slippage,
            "average_slippage_bps": self.average_slippage_bps,
            "peak_exposure": self.peak_exposure,
            "average_exposure": self.average_exposure,
            "total_turnover": self.total_turnover,
            "total_turnover_cost": self.total_turnover_cost,
            "max_drawdown": self.max_drawdown,
            "benchmark_divergence": self.benchmark_divergence,
            "equity_curve": dict(self.equity_curve),
        }

    def to_json(self) -> str:
        """Serialize as deterministic JSON."""
        import json

        return json.dumps(self.to_dict(), sort_keys=True, default=str)


def _validate_benchmark(
    benchmark_returns: pd.Series, equity_index: pd.DatetimeIndex
) -> pd.Series:
    if not isinstance(benchmark_returns, pd.Series):
        raise ResearchInputError("benchmark_returns must be a pandas Series")
    numeric = pd.to_numeric(benchmark_returns, errors="coerce")
    if numeric.isna().any():
        raise ResearchInputError("benchmark_returns must not contain missing values")
    # Benchmark returns are accumulated over the same dates as the equity
    # curve; reindexing to the intersection keeps the comparison honest.
    return numeric.reindex(equity_index).fillna(0.0)


def compute_paper_analytics(
    fills: Sequence[Fill],
    marks: Sequence[PositionMark],
    prices: Mapping[str, float] | None = None,
    *,
    initial_cash: float = 1_000_000.0,
    benchmark_returns: pd.Series | None = None,
) -> PaperAnalytics:
    """Compute deterministic realized/unrealized PnL, costs, and risk stats.

    Accounting

    * Realized PnL uses FIFO lot matching per symbol over fills sorted by
      timestamp (then order id) — deterministic and auditable.
    * Unrealized PnL uses the latest mark price (or the supplied ``prices``
      for symbols without a mark) against average cost.
    * Slippage is measured per fill against ``reference_price`` in bps and
      aggregated in currency units.
    * Turnover is one-way traded notional; turnover cost is the realized
      slippage against the reference price.

    The equity curve is ``cash + mark-to-market positions`` at the latest
    mark date; when ``benchmark_returns`` is supplied, benchmark divergence
    is the difference between cumulative strategy return and cumulative
    benchmark return over the same span.
    """
    if initial_cash <= 0 or not np.isfinite(initial_cash):
        raise ResearchInputError("initial_cash must be finite and positive")
    if not fills:
        raise ResearchInputError("at least one fill is required")
    ordered = sorted(fills, key=lambda fill: (fill.timestamp, fill.order_id))

    # -- FIFO realized PnL and turnover --------------------------------------
    lots: dict[str, list[list[float]]] = {}
    realized: dict[str, float] = {}
    turnover_per_side: dict[str, float] = {}
    total_slippage = 0.0
    slippage_bps_values: list[float] = []
    for fill in ordered:
        buy_value = fill.quantity * fill.price
        turnover_per_side[fill.symbol] = (
            turnover_per_side.get(fill.symbol, 0.0) + buy_value
        )
        if fill.reference_price is not None:
            slip = (fill.price - fill.reference_price) / fill.reference_price * 10_000
            slippage_bps_values.append(float(slip))
            total_slippage += fill.quantity * (fill.price - fill.reference_price)
        if fill.side == "BUY":
            lots.setdefault(fill.symbol, []).append([fill.quantity, fill.price])
            continue
        remaining = fill.quantity
        symbol_lots = lots.setdefault(fill.symbol, [])
        pnl = 0.0
        while remaining > 1e-12 and symbol_lots:
            lot_quantity, lot_price = symbol_lots[0]
            matched = min(remaining, lot_quantity)
            pnl += matched * (fill.price - lot_price)
            remaining -= matched
            lot_quantity -= matched
            if lot_quantity <= 1e-12:
                symbol_lots.pop(0)
            else:
                symbol_lots[0][0] = lot_quantity
        realized[fill.symbol] = realized.get(fill.symbol, 0.0) + pnl

    # -- unrealized PnL from marks --------------------------------------------
    latest_marks: dict[str, tuple[pd.Timestamp, float, float]] = {}
    for mark in marks:
        if (
            mark.timestamp
            > latest_marks.get(mark.symbol, (pd.Timestamp.min, 0.0, 0.0))[0]
        ):
            latest_marks[mark.symbol] = (
                mark.timestamp,
                mark.quantity,
                mark.average_price,
            )
    unrealized: dict[str, float] = {}
    equity_dates = sorted({fill.timestamp for fill in ordered})
    for symbol, (_, quantity, average_price) in latest_marks.items():
        if quantity <= 0:
            continue
        mark_price = float((prices or {}).get(symbol, average_price))
        unrealized[symbol] = quantity * (mark_price - average_price)

    # -- equity curve and exposure ---------------------------------------------
    equity_map: dict[str, float] = {}
    exposure_curve: list[float] = []
    for date in equity_dates:
        # Reconstruct cash as of ``date`` from the ordered fills.
        as_of_cash = initial_cash
        for fill in ordered:
            if fill.timestamp > date:
                break
            as_of_cash += (
                (-fill.quantity * fill.price)
                if fill.side == "BUY"
                else (fill.quantity * fill.price)
            )
        positions_value = 0.0
        for symbol, (_, quantity, average_price) in latest_marks.items():
            if quantity <= 0:
                continue
            mark_price = float((prices or {}).get(symbol, average_price))
            positions_value += quantity * mark_price
        equity = float(as_of_cash + positions_value)
        equity_map[date.isoformat()] = equity
        exposure_curve.append(positions_value / equity if equity > 0 else 0.0)

    equity_values = np.array(list(equity_map.values()), dtype=float)
    if len(equity_values) == 0:
        equity_values = np.array([initial_cash], dtype=float)
    peaks = np.maximum.accumulate(equity_values)
    drawdowns = (
        np.divide(
            equity_values,
            peaks,
            out=np.ones_like(equity_values),
            where=peaks > 0,
        )
        - 1.0
    )
    max_drawdown = float(drawdowns.min())

    total_turnover = float(sum(turnover_per_side.values()))
    benchmark_divergence = 0.0
    if benchmark_returns is not None and equity_map:
        index = pd.DatetimeIndex(equity_dates)
        benchmark = _validate_benchmark(benchmark_returns, index)
        strategy_cumulative = float((equity_values[-1] / initial_cash) - 1.0)
        benchmark_cumulative = float((1.0 + benchmark).prod() - 1.0)
        benchmark_divergence = strategy_cumulative - benchmark_cumulative

    total_realized = float(sum(realized.values()))
    total_unrealized = float(sum(unrealized.values()))
    return PaperAnalytics(
        realized_pnl={name: float(value) for name, value in realized.items()},
        unrealized_pnl={name: float(value) for name, value in unrealized.items()},
        total_realized_pnl=total_realized,
        total_unrealized_pnl=total_unrealized,
        total_slippage=float(total_slippage),
        average_slippage_bps=(
            float(np.mean(slippage_bps_values)) if slippage_bps_values else 0.0
        ),
        peak_exposure=max(exposure_curve),
        average_exposure=float(np.mean(exposure_curve)),
        total_turnover=total_turnover,
        total_turnover_cost=float(total_slippage),
        max_drawdown=max_drawdown,
        benchmark_divergence=benchmark_divergence,
        equity_curve=equity_map,
    )
