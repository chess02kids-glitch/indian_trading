"""The single deterministic order-validation choke point.

Every order that may reach an execution adapter passes through
:func:`validate_order_intent`. This is a hard code-level invariant, not a
configuration: ``OrderType`` has exactly one member (``LIMIT``), and any
other order type (MARKET, IOC, ...) is rejected before it can proceed.
Invalid input is never silently converted to LIMIT — it fails explicitly.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from models.domain import (
    ExecutionMode,
    OrderIntent,
    OrderSide,
    OrderType,
)

__all__ = [
    "ExecutionBlockedError",
    "OrderValidationError",
    "validate_limit_price_band",
    "validate_order_intent",
    "validate_order_intents",
]


class OrderValidationError(ValueError):
    """Raised when an order intent violates a safety invariant."""


class ExecutionBlockedError(RuntimeError):
    """Raised when execution is attempted in a mode that is not permitted."""


def _reject(message: str) -> None:
    raise OrderValidationError(message)


def validate_order_intent(order: Any, *, now: datetime | None = None) -> OrderIntent:
    """Validate one order intent; return it unchanged when it is safe.

    Rejects (explicitly, never converting):

    * order types other than LIMIT (MARKET, IOC, LIMIT_WITH_MARKET, ...)
    * invalid or non-positive quantity (fractional shares are rejected)
    * invalid limit price (missing, non-finite, non-positive)
    * invalid symbol or exchange
    * invalid side
    * missing identifiers (internal order id, idempotency key, strategy,
      hypothesis)
    * future timestamps beyond a small clock-skew tolerance
    """
    if not isinstance(order, OrderIntent):
        # Accept plain dicts so callers cannot bypass construction-time checks
        # by pre-building a "validated" object; re-validate from scratch.
        try:
            order = OrderIntent.model_validate(dict(order))
        except Exception as exc:
            _reject(f"order intent is invalid: {exc}")

    if order.order_type is not OrderType.LIMIT:
        _reject(f"only LIMIT orders are permitted, got {order.order_type!r}")
    if order.order_type.value != "LIMIT":
        _reject(f"only LIMIT orders are permitted, got {order.order_type.value!r}")

    if not isinstance(order.side, OrderSide):
        _reject(f"invalid side: {order.side!r}")

    if isinstance(order.quantity, bool) or not isinstance(order.quantity, int):
        _reject(f"quantity must be a whole number of shares, got {order.quantity!r}")
    if order.quantity <= 0:
        _reject(f"quantity must be positive, got {order.quantity}")
    if order.target_position is not None:
        if isinstance(order.target_position, bool) or not isinstance(
            order.target_position, int
        ):
            _reject(
                "target_position must be a whole number of shares, "
                f"got {order.target_position!r}"
            )
        if order.target_position < 0:
            _reject(f"target_position must be >= 0, got {order.target_position}")

    price = order.limit_price
    if not isinstance(price, (int, float)) or isinstance(price, bool):
        _reject(f"limit_price must be numeric, got {price!r}")
    if not math.isfinite(price) or price <= 0:
        _reject(f"limit_price must be finite and positive, got {price!r}")

    for field_name in (
        "internal_order_id",
        "idempotency_key",
        "strategy_id",
        "hypothesis_id",
    ):
        value = getattr(order, field_name)
        if not isinstance(value, str) or not value.strip():
            _reject(f"{field_name} must be a non-empty string")

    if not isinstance(order.symbol, str) or not order.symbol.strip():
        _reject("symbol must be a non-empty string")

    if not isinstance(order.timestamp, datetime):
        _reject("timestamp must be a datetime")
    if now is not None:
        reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        candidate = (
            order.timestamp
            if order.timestamp.tzinfo is not None
            else order.timestamp.replace(tzinfo=timezone.utc)
        )
        if (candidate - reference).total_seconds() > 60:
            _reject("order timestamp is more than 60 seconds in the future")

    return order


def validate_order_intents(orders: Mapping[str, Any] | list[Any]) -> list[OrderIntent]:
    """Validate a batch; the first invalid order fails the whole batch."""
    if isinstance(orders, Mapping):
        items: list[Any] = [dict(order) for order in orders.values()]
    else:
        items = list(orders)
    return [validate_order_intent(order) for order in items]


def validate_limit_price_band(
    intent: OrderIntent,
    reference_price: float,
    *,
    band_fraction: float = 0.1,
) -> None:
    """Reject a limit price too far from the current reference price.

    A LIMIT order is only safe when its price is plausibly executable; a limit
    far from market indicates a bad calculation and must fail explicitly.
    """
    try:
        reference = float(reference_price)
    except (TypeError, ValueError):
        _reject(f"reference price must be numeric, got {reference_price!r}")
    if not math.isfinite(reference) or reference <= 0:
        _reject("reference price must be finite and positive")
    if band_fraction <= 0 or band_fraction > 1:
        _reject("band_fraction must be in (0, 1]")
    deviation = abs(intent.limit_price - reference) / reference
    if deviation > band_fraction:
        _reject(
            f"limit price {intent.limit_price} deviates {deviation:.1%} from "
            f"reference {reference}, exceeding {band_fraction:.1%} band"
        )


def validate_execution_mode(mode: str | ExecutionMode) -> ExecutionMode:
    """Permit only RESEARCH / PAPER / SANDBOX operating modes."""
    try:
        normalized = ExecutionMode(str(mode).strip().upper())
    except ValueError:
        _reject(
            f"execution mode {mode!r} is not permitted (RESEARCH/PAPER/SANDBOX only)"
        )
    return normalized
