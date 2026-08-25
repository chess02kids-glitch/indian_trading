"""Frozen broker-side data models.

These models describe what a *broker adapter* returns: profiles, funds,
holdings, quotes, broker order records, and trade records. Order *requests*
are never modelled here — the only order-request vocabulary in the system is
:mod:`models.domain` (``OrderIntent`` / ``OrderResult``), so there is exactly
one order safety boundary and it stays LIMIT-only.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from models.domain import VALID_EXCHANGES, OrderSide, OrderStatus

__all__ = [
    "BrokerProfile",
    "FundsSummary",
    "Holding",
    "Quote",
    "BrokerOrderRecord",
    "TradeRecord",
]


class _FrozenModel(BaseModel):
    """Base for immutable, strict broker-side objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class BrokerProfile(_FrozenModel):
    """Broker account profile, as reported by the sandbox."""

    broker: str
    client_id: str
    user_name: str
    email: str | None = None
    exchanges: tuple[str, ...] = ()
    products: tuple[str, ...] = ()

    @field_validator("broker", "client_id", "user_name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty string")
        return value


class FundsSummary(_FrozenModel):
    """Available funds and margin utilisation."""

    broker: str
    available_cash: float
    used_margin: float = 0.0
    currency: str = "INR"
    as_of: datetime | None = None

    @field_validator("available_cash", "used_margin")
    @classmethod
    def _finite_amount(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cash amounts must be finite")
        return value


class Holding(_FrozenModel):
    """One delivery holding reported by the broker."""

    broker: str
    symbol: str
    exchange: str = "NSE"
    quantity: int = 0
    average_price: float | None = None

    @field_validator("quantity")
    @classmethod
    def _non_negative_quantity(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("quantity must be a non-negative whole number")
        return value

    @field_validator("exchange")
    @classmethod
    def _valid_exchange(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in VALID_EXCHANGES:
            raise ValueError(f"exchange must be one of {sorted(VALID_EXCHANGES)}")
        return normalized


class Quote(_FrozenModel):
    """A last-traded-price quote for one instrument."""

    broker: str
    symbol: str
    exchange: str = "NSE"
    last_price: float
    bid_price: float | None = None
    ask_price: float | None = None
    timestamp: datetime | None = None

    @field_validator("last_price", "bid_price", "ask_price")
    @classmethod
    def _positive_price(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("prices must be finite and strictly positive")
        return value


class BrokerOrderRecord(_FrozenModel):
    """The broker's view of one order (status, fills, identifiers).

    ``status`` uses the domain ``OrderStatus`` vocabulary — adapters map
    broker-specific wire strings at the boundary; ``raw_status`` preserves the
    original broker string for dashboards and forensics.
    """

    broker: str
    order_id: str
    tag: str | None = None
    idempotency_key: str | None = None
    symbol: str
    exchange: str = "NSE"
    side: OrderSide
    order_type: str = "LIMIT"
    quantity: int
    price: float
    status: OrderStatus
    raw_status: str = ""
    filled_quantity: int = 0
    average_price: float | None = None
    placed_at: datetime | None = None
    updated_at: datetime | None = None
    message: str | None = None
    duplicate: bool = False

    @field_validator("order_type")
    @classmethod
    def _limit_only(cls, value: str) -> str:
        # Wire-level LIMIT guard: a broker record can never describe a MARKET
        # or IOC order, because this system cannot have placed one.
        if str(value).strip().upper() != "LIMIT":
            raise ValueError("broker order records must be LIMIT only")
        return "LIMIT"

    @field_validator("quantity", "filled_quantity")
    @classmethod
    def _non_negative_quantity(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("quantities must be non-negative whole numbers")
        return value

    @field_validator("price", "average_price")
    @classmethod
    def _positive_price(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("prices must be finite and strictly positive")
        return value


class TradeRecord(_FrozenModel):
    """One executed trade reported by the broker."""

    broker: str
    trade_id: str
    order_id: str
    symbol: str
    exchange: str = "NSE"
    side: OrderSide
    quantity: int
    price: float
    traded_at: datetime | None = None

    @field_validator("quantity")
    @classmethod
    def _positive_quantity(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("trade quantity must be a positive whole number")
        return value

    @field_validator("price")
    @classmethod
    def _positive_price(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("trade price must be finite and strictly positive")
        return value

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view for status documents and dashboards."""
        return {
            "broker": self.broker,
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side.value,
            "quantity": self.quantity,
            "price": self.price,
            "traded_at": self.traded_at.isoformat() if self.traded_at else None,
        }
