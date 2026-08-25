"""Broker-side model invariants (wire safety + immutability)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from broker.models import (
    BrokerOrderRecord,
    FundsSummary,
    Holding,
    Quote,
    TradeRecord,
)
from models.domain import OrderSide, OrderStatus

_BASE_ORDER = {
    "broker": "upstox",
    "order_id": "upstox-sbx-1",
    "symbol": "RELIANCE",
    "side": OrderSide.BUY,
    "quantity": 10,
    "price": 100.0,
    "status": OrderStatus.FILLED,
}


class TestBrokerOrderRecord:
    def test_limit_record_ok(self) -> None:
        record = BrokerOrderRecord(**_BASE_ORDER)
        assert record.order_type == "LIMIT"

    @pytest.mark.parametrize(
        "bad_type", ["MARKET", "IOC", "market", "ioc", "SL", "SL-M"]
    )
    def test_non_limit_wire_type_rejected(self, bad_type: str) -> None:
        """A broker record can never describe a MARKET/IOC order."""
        with pytest.raises(ValidationError):
            BrokerOrderRecord(**{**_BASE_ORDER, "order_type": bad_type})

    def test_frozen(self) -> None:
        record = BrokerOrderRecord(**_BASE_ORDER)
        with pytest.raises(ValidationError):
            record.filled_quantity = 5  # type: ignore[misc]

    def test_negative_fills_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BrokerOrderRecord(**{**_BASE_ORDER, "filled_quantity": -1})


class TestQuote:
    def test_valid_quote(self) -> None:
        quote = Quote(broker="dhan", symbol="SBIN", last_price=700.0, bid_price=699.5)
        assert quote.exchange == "NSE"

    @pytest.mark.parametrize("bad_price", [0.0, -1.0, float("nan"), float("inf")])
    def test_bad_prices_rejected(self, bad_price: float) -> None:
        with pytest.raises(ValidationError):
            Quote(broker="dhan", symbol="SBIN", last_price=bad_price)


class TestFundsSummary:
    def test_nan_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FundsSummary(broker="upstox", available_cash=float("nan"))

    def test_valid(self) -> None:
        funds = FundsSummary(broker="upstox", available_cash=10.0, used_margin=2.5)
        assert funds.currency == "INR"


class TestHoldingAndTrade:
    def test_holding_quantity_guard(self) -> None:
        with pytest.raises(ValidationError):
            Holding(broker="dhan", symbol="TCS", quantity=-3)

    def test_holding_exchange_guard(self) -> None:
        with pytest.raises(ValidationError):
            Holding(broker="dhan", symbol="TCS", exchange="NYSE", quantity=1)

    def test_trade_quantity_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            TradeRecord(
                broker="upstox",
                trade_id="t-1",
                order_id="o-1",
                symbol="INFY",
                side=OrderSide.SELL,
                quantity=0,
                price=50.0,
            )

    def test_trade_serialisation(self) -> None:
        trade = TradeRecord(
            broker="upstox",
            trade_id="t-1",
            order_id="o-1",
            symbol="INFY",
            side=OrderSide.BUY,
            quantity=1,
            price=50.0,
        )
        assert trade.to_dict()["side"] == "BUY"
