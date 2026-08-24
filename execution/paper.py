"""Deterministic paper-trading execution adapter.

Simulates a broker with realistic outcomes: full fills, partial fills,
rejections (price band, insufficient cash), cancellations, timeouts, and
duplicate-request detection. There is no network access and no
credentials: this adapter cannot touch live capital by construction.

Determinism: all randomness comes from an explicitly seeded generator and
all time comes from an explicitly supplied clock, so the same sequence of
calls always produces the same outcome.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from models.domain import (
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
)

from .validation import validate_limit_price_band, validate_order_intent

__all__ = ["PaperBroker", "PaperBrokerConfig"]


class PaperBrokerConfig:
    """Knobs for the paper broker. Defaults are conservative."""

    def __init__(
        self,
        *,
        seed: int = 42,
        fill_probability: float = 1.0,
        partial_fill_probability: float = 0.2,
        partial_fill_fraction: float = 0.5,
        price_band_fraction: float = 0.10,
        initial_cash: float = 1_000_000.0,
        order_ttl_days: float = 2.0,
        max_pending_orders: int = 100,
    ) -> None:
        if not 0 <= fill_probability <= 1:
            raise ValueError("fill_probability must be in [0, 1]")
        if not 0 <= partial_fill_probability <= 1:
            raise ValueError("partial_fill_probability must be in [0, 1]")
        if not 0 < partial_fill_fraction <= 1:
            raise ValueError("partial_fill_fraction must be in (0, 1]")
        if not 0 < price_band_fraction <= 1:
            raise ValueError("price_band_fraction must be in (0, 1]")
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if order_ttl_days <= 0:
            raise ValueError("order_ttl_days must be positive")
        if max_pending_orders < 1:
            raise ValueError("max_pending_orders must be positive")
        self.seed = int(seed)
        self.fill_probability = float(fill_probability)
        self.partial_fill_probability = float(partial_fill_probability)
        self.partial_fill_fraction = float(partial_fill_fraction)
        self.price_band_fraction = float(price_band_fraction)
        self.initial_cash = float(initial_cash)
        self.order_ttl_days = float(order_ttl_days)
        self.max_pending_orders = int(max_pending_orders)


class PaperBroker:
    """Deterministic simulated broker for PAPER and SANDBOX modes."""

    def __init__(
        self,
        clock: datetime,
        config: PaperBrokerConfig | None = None,
        *,
        seed: int | None = None,
    ) -> None:
        self.config = config or PaperBrokerConfig()
        effective_seed = self.config.seed if seed is None else int(seed)
        self._rng = np.random.default_rng(effective_seed)
        self._clock = clock
        self._cash = self.config.initial_cash
        self._orders: dict[str, OrderResult] = {}
        self._positions: dict[tuple[str, str], Position] = {}
        self._by_key: dict[str, str] = {}

    # -- clock -------------------------------------------------------------

    @property
    def clock(self) -> datetime:
        return self._clock

    def advance_clock(self, to: datetime) -> None:
        """Move the simulated clock forward; expires timed-out orders."""
        if to < self._clock:
            raise ValueError("clock cannot move backwards")
        self._clock = to
        self._expire_stale_orders()

    def _now(self) -> datetime:
        return self._clock

    # -- adapter interface ---------------------------------------------------

    def submit_order(self, intent: OrderIntent, reference_price: float) -> OrderResult:
        """Validate, de-duplicate, and simulate one order."""
        validated = validate_order_intent(intent, now=self._now())
        existing = self._by_key.get(validated.idempotency_key)
        if existing is not None:
            prior = self._orders.get(existing)
            if prior is not None:
                # Duplicate request: return the original outcome, create nothing.
                return prior
        validate_limit_price_band(
            validated,
            reference_price,
            band_fraction=self.config.price_band_fraction,
        )

        result = self._simulate(validated)
        self._orders[result.internal_order_id] = result
        self._by_key.setdefault(validated.idempotency_key, result.internal_order_id)
        return result

    def cancel_order(self, internal_order_id: str) -> OrderResult | None:
        result = self._orders.get(internal_order_id)
        if result is None:
            return None
        if result.status not in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
            return result
        cancelled = self._make_result(
            internal_order_id=internal_order_id,
            idempotency_key=result.idempotency_key,
            broker_order_id=result.broker_order_id,
            symbol=result.symbol,
            side=result.side,
            status=OrderStatus.CANCELLED,
            requested_quantity=result.requested_quantity,
            filled_quantity=result.filled_quantity,
            average_fill_price=result.average_fill_price,
            reason="cancelled by operator",
        )
        self._orders[internal_order_id] = cancelled
        return cancelled

    def get_order_status(self, internal_order_id: str) -> OrderResult | None:
        return self._orders.get(internal_order_id)

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_open_orders(self) -> list[OrderResult]:
        return [
            result
            for result in self._orders.values()
            if result.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]

    def get_cash(self) -> float:
        return self._cash

    def get_state(self) -> dict[str, Any]:
        """Machine-readable snapshot for dashboards and reconciliation."""
        return {
            "cash": self._cash,
            "positions": [
                {
                    "symbol": p.symbol,
                    "exchange": p.exchange,
                    "quantity": p.quantity,
                    "average_price": p.average_price,
                }
                for p in self.get_positions()
            ],
            "open_orders": [
                {
                    "internal_order_id": o.internal_order_id,
                    "symbol": o.symbol,
                    "status": o.status.value,
                    "filled_quantity": o.filled_quantity,
                }
                for o in self.get_open_orders()
            ],
            "orders_total": len(self._orders),
        }

    # -- simulation -----------------------------------------------------------

    def _simulate(self, intent: OrderIntent) -> OrderResult:
        if len(self.get_open_orders()) >= self.config.max_pending_orders:
            return self._rejected(intent, "too many pending orders")

        if intent.side is OrderSide.BUY:
            cost = intent.quantity * intent.limit_price
            if cost > self._cash:
                return self._rejected(intent, "insufficient cash")
        else:
            held = self._position_for(intent).quantity
            if intent.quantity > held:
                return self._rejected(intent, "insufficient position to sell")

        draw = float(self._rng.random())
        if draw >= self.config.fill_probability:
            # No immediate fill: order stays PENDING (may fill/expire later).
            return self._make_result(
                internal_order_id=intent.internal_order_id,
                idempotency_key=intent.idempotency_key,
                broker_order_id=f"paper-{intent.internal_order_id}",
                symbol=intent.symbol,
                side=intent.side,
                status=OrderStatus.PENDING,
                requested_quantity=intent.quantity,
                filled_quantity=0,
                average_fill_price=None,
                reason="awaiting fill",
            )
        fill_draw = float(self._rng.random())
        if fill_draw >= self.config.partial_fill_probability:
            filled = intent.quantity
        else:
            filled = max(
                1,
                int(np.floor(intent.quantity * self.config.partial_fill_fraction)),
            )
        status = (
            OrderStatus.FILLED
            if filled == intent.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        self._apply_fill(intent, filled, intent.limit_price)
        return self._make_result(
            internal_order_id=intent.internal_order_id,
            idempotency_key=intent.idempotency_key,
            broker_order_id=f"paper-{intent.internal_order_id}",
            symbol=intent.symbol,
            side=intent.side,
            status=status,
            requested_quantity=intent.quantity,
            filled_quantity=filled,
            average_fill_price=intent.limit_price if filled else None,
            reason=None,
        )

    def _apply_fill(self, intent: OrderIntent, filled: int, price: float) -> None:
        if filled <= 0:
            return
        key = (intent.symbol, intent.exchange)
        position = self._position_for(intent)
        if intent.side is OrderSide.BUY:
            total_cost = (
                position.quantity * (position.average_price or 0.0) + filled * price
            )
            new_quantity = position.quantity + filled
            new_average = total_cost / new_quantity
            self._cash -= filled * price
        else:
            new_quantity = position.quantity - filled
            self._cash += filled * price
            new_average = position.average_price
        self._positions[key] = Position(
            symbol=intent.symbol,
            exchange=intent.exchange,
            quantity=new_quantity,
            average_price=new_average,
            updated_at=self._now(),
        )

    def _expire_stale_orders(self) -> None:
        ttl = timedelta(days=self.config.order_ttl_days)
        for order_id, result in list(self._orders.items()):
            if result.status is not OrderStatus.PENDING:
                continue
            if self._clock - result.timestamp > ttl:
                expired = self._make_result(
                    internal_order_id=order_id,
                    idempotency_key=result.idempotency_key,
                    broker_order_id=result.broker_order_id,
                    symbol=result.symbol,
                    side=result.side,
                    status=OrderStatus.EXPIRED,
                    requested_quantity=result.requested_quantity,
                    filled_quantity=result.filled_quantity,
                    average_fill_price=result.average_fill_price,
                    reason="order expired without fill",
                )
                self._orders[order_id] = expired

    def _position_for(self, intent: OrderIntent) -> Position:
        key = (intent.symbol, intent.exchange)
        if key not in self._positions:
            self._positions[key] = Position(
                symbol=intent.symbol, exchange=intent.exchange, quantity=0
            )
        return self._positions[key]

    def _rejected(self, intent: OrderIntent, reason: str) -> OrderResult:
        return OrderResult.model_validate(
            {
                "internal_order_id": intent.internal_order_id,
                "idempotency_key": intent.idempotency_key,
                "symbol": intent.symbol,
                "side": intent.side,
                "status": OrderStatus.REJECTED,
                "requested_quantity": intent.quantity,
                "filled_quantity": 0,
                "average_fill_price": None,
                "timestamp": self._clock,
                "reason": reason,
            }
        )

    def _make_result(self, **fields: Any) -> OrderResult:
        fields.setdefault("timestamp", self._now())
        return OrderResult.model_validate(fields)
