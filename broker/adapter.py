"""Broker adapter implementations.

``BrokerAdapter`` (re-exported from :mod:`broker.interface`) is the single
abstraction every broker integration implements. This module ships the two
sandbox adapters, ``UpstoxAdapter`` and ``DhanAdapter``, which mirror the
same interface over their respective wire dialects and are therefore fully
interchangeable behind the safe-execution layer.

Every adapter here is sandbox-only:

* OAuth is a *skeleton*: ``login_url`` produces a sandbox authorization URL
  and ``complete_login`` exchanges a manually pasted code for a sandbox
  token. Login is never automated.
* Tokens are stored via :class:`broker.token.TokenManager`
  (:class:`broker.token.FileTokenStore`, owner-only files).
* All requests go through a :class:`broker.transport.SimulatedSandboxTransport`
  (deterministic, in-process) unless a caller injects an alternative sandbox
  transport. There is no production transport: ``LIVE`` mode refuses at
  construction, at token check, and at submission.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from broker.errors import (
    BrokerConfigurationError,
    BrokerResponseError,
    LiveTradingDisabledError,
    SandboxOnlyError,
)
from broker.interface import BrokerAdapter
from broker.mode import OperatingMode, check_execution_permitted
from broker.models import (
    BrokerOrderRecord,
    BrokerProfile,
    FundsSummary,
    Holding,
    Quote,
    TradeRecord,
)
from broker.token import TokenManager, TokenRecord
from broker.transport import SimulatedSandboxTransport, validate_sandbox_base_url
from execution.validation import validate_order_intent
from models.domain import OrderIntent, OrderSide, OrderStatus, Position

__all__ = [
    "BrokerAdapter",
    "BaseSandboxAdapter",
    "UpstoxAdapter",
    "DhanAdapter",
    "SUPPORTED_BROKERS",
    "create_adapter",
]

SUPPORTED_BROKERS = ("upstox", "dhan")


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value))


class BaseSandboxAdapter(BrokerAdapter):
    """Shared sandbox implementation; subclasses provide the wire dialect."""

    flavor = "sandbox"

    def __init__(
        self,
        *,
        transport: Any = None,
        token_manager: TokenManager | None = None,
        mode: OperatingMode = OperatingMode.SANDBOX,
        base_url: str | None = None,
    ) -> None:
        # Hard gate 1: LIVE refuses at construction; RESEARCH/PAPER have no
        # reason to hold a broker adapter either (sandbox is enforced here).
        if mode is OperatingMode.LIVE:
            raise LiveTradingDisabledError(
                "cannot construct a broker adapter in LIVE mode: live execution "
                "is disabled by policy"
            )
        if mode is not OperatingMode.SANDBOX:
            raise SandboxOnlyError(
                f"{self.__class__.__name__} only operates in SANDBOX mode, "
                f"got {mode.value}"
            )
        self._mode = mode
        self._base_url = (
            validate_sandbox_base_url(base_url)
            if base_url
            else (f"simulated://{self.flavor}")
        )
        self._transport = transport or SimulatedSandboxTransport(self.flavor)
        self._tokens = token_manager or TokenManager()
        self._rehydrate_session()

    # -- plumbing ---------------------------------------------------------

    @property
    def broker_name(self) -> str:
        return self.flavor

    @property
    def mode(self) -> OperatingMode:
        return self._mode

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token_manager(self) -> TokenManager:
        return self._tokens

    @property
    def transport(self) -> Any:
        return self._transport

    def _rehydrate_session(self) -> None:
        """Re-register a locally stored token with the sandbox backend."""
        record = self._tokens.get_record(self.broker_name)
        if record is not None and hasattr(self._transport, "register_session"):
            self._transport.register_session(record.access_token, record.expires_at)

    def _token(self) -> str:
        """Client-side expiry gate: raises StaleTokenError before any request."""
        return self._tokens.get_token(self.broker_name)

    def _extract(self, response: Any) -> Any:
        """Unwrap flavor envelopes: Upstox ``{"status","data"}``; Dhan bare."""
        if response is None:
            return None
        if isinstance(response, Mapping) and "status" in response:
            if response.get("status") != "success":
                raise BrokerResponseError(f"broker error response: {response!r}")
            return response.get("data")
        return response

    # -- authentication -----------------------------------------------------

    def login_url(self, state: str) -> str:
        """Sandbox OAuth skeleton: URL a human visits to obtain a code."""
        if not isinstance(state, str) or not state.strip():
            raise BrokerConfigurationError("state must be a non-empty string")
        return f"{self._base_url}/oauth/authorize?state={state.strip()}"

    def complete_login(self, code: str) -> TokenRecord:
        """Exchange a manually obtained code; stores the token (never automated)."""
        payload = self._transport.exchange_code(code)
        return self._tokens.record_token(
            self.broker_name,
            str(payload["access_token"]),
            expires_in_seconds=float(payload["expires_in"]),
        )

    def is_authenticated(self) -> bool:
        return self._tokens.status(self.broker_name).state in (
            "active",
            "expiring_soon",
        )

    # -- account reads -------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._transport.request("ping")
        except Exception:
            return False
        return True

    def get_profile(self) -> BrokerProfile:
        data = self._extract(self._transport.request("profile", token=self._token()))
        return self._parse_profile(dict(data))

    def get_funds(self) -> FundsSummary:
        data = self._extract(self._transport.request("funds", token=self._token()))
        return self._parse_funds(dict(data))

    def get_holdings(self) -> list[Holding]:
        data = self._extract(self._transport.request("holdings", token=self._token()))
        return [self._parse_holding(dict(item)) for item in data or []]

    def get_positions(self) -> list[Position]:
        data = self._extract(self._transport.request("positions", token=self._token()))
        positions: list[Position] = []
        for item in data or []:
            fields = self._position_fields(dict(item))
            positions.append(
                Position(
                    symbol=str(fields["symbol"]).upper(),
                    exchange=str(fields.get("exchange", "NSE")).upper(),
                    quantity=int(fields["quantity"]),
                    average_price=fields.get("average_price"),
                    updated_at=self._tokens.now(),
                )
            )
        return positions

    def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        data = self._extract(
            self._transport.request(
                "quote",
                payload={"symbol": symbol, "exchange": exchange},
                token=self._token(),
            )
        )
        return self._parse_quote(dict(data), symbol=symbol, exchange=exchange)

    # -- orders ----------------------------------------------------------------

    def place_limit_order(self, intent: OrderIntent) -> BrokerOrderRecord:
        # Hard gate 2: re-check mode and LIMIT-only at the adapter boundary,
        # even though ExecutionService and the safe-execution layer already
        # validated. Defense in depth at the point of broker submission.
        check_execution_permitted(self._mode)
        validated = validate_order_intent(intent)
        payload = self._build_place_payload(validated)
        data = self._extract(
            self._transport.request("place", payload=payload, token=self._token())
        )
        return self._parse_order(dict(data))

    def get_order_status(self, order_ref: str) -> BrokerOrderRecord | None:
        data = self._extract(
            self._transport.request(
                "status", payload={"ref": order_ref}, token=self._token()
            )
        )
        if data is None:
            return None
        return self._parse_order(dict(data))

    def cancel_order(self, order_ref: str) -> BrokerOrderRecord | None:
        data = self._extract(
            self._transport.request(
                "cancel", payload={"ref": order_ref}, token=self._token()
            )
        )
        if data is None:
            return None
        return self._parse_order(dict(data))

    def get_trade_history(self) -> list[TradeRecord]:
        data = self._extract(self._transport.request("trades", token=self._token()))
        return [self._parse_trade(dict(item)) for item in data or []]

    # -- wire dialect hooks (subclasses) ---------------------------------------

    def _position_fields(self, item: dict[str, Any]) -> dict[str, Any]:
        """Canonicalise a wire position into symbol/exchange/quantity/avg."""
        raise NotImplementedError

    def _build_place_payload(self, intent: OrderIntent) -> dict[str, Any]:
        raise NotImplementedError

    def _parse_profile(self, data: dict[str, Any]) -> BrokerProfile:
        raise NotImplementedError

    def _parse_funds(self, data: dict[str, Any]) -> FundsSummary:
        raise NotImplementedError

    def _parse_holding(self, data: dict[str, Any]) -> Holding:
        raise NotImplementedError

    def _parse_quote(
        self, data: dict[str, Any], *, symbol: str, exchange: str
    ) -> Quote:
        raise NotImplementedError

    def _parse_order(self, data: dict[str, Any]) -> BrokerOrderRecord:
        raise NotImplementedError

    def _parse_trade(self, data: dict[str, Any]) -> TradeRecord:
        raise NotImplementedError


class UpstoxAdapter(BaseSandboxAdapter):
    """Upstox API v2 sandbox adapter (simulated transport wire dialect)."""

    flavor = "upstox"

    _STATUS_MAP = {
        "open": None,  # resolved with filled quantity below
        "complete": OrderStatus.FILLED,
        "cancelled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
        "expired": OrderStatus.EXPIRED,
        "trigger pending": OrderStatus.PENDING,
    }

    def _position_fields(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": item["symbol"],
            "exchange": item.get("exchange", "NSE"),
            "quantity": item["quantity"],
            "average_price": item.get("average_price"),
        }

    def _build_place_payload(self, intent: OrderIntent) -> dict[str, Any]:
        return {
            "quantity": intent.quantity,
            "product": "D",
            "validity": "DAY",
            "price": intent.limit_price,
            "tag": intent.internal_order_id,
            "idempotency_key": intent.idempotency_key,
            "symbol": intent.symbol,
            "exchange": intent.exchange,
            "side": intent.side.value,
            "order_type": "LIMIT",
            "disclosed_quantity": 0,
            "trigger_price": 0,
            "is_amo": False,
        }

    def _parse_profile(self, data: dict[str, Any]) -> BrokerProfile:
        return BrokerProfile(
            broker=self.flavor,
            client_id=str(data.get("client_id") or data.get("user_id")),
            user_name=str(data.get("user_name", "Unknown")),
            email=data.get("email"),
            exchanges=tuple(data.get("exchanges", ())),
            products=tuple(data.get("products", ())),
        )

    def _parse_funds(self, data: dict[str, Any]) -> FundsSummary:
        equity = dict(data.get("equity", data))
        return FundsSummary(
            broker=self.flavor,
            available_cash=float(
                equity.get("available_margin", equity.get("available_cash", 0.0))
            ),
            used_margin=float(equity.get("used_margin", 0.0)),
            currency=str(data.get("currency", "INR")),
            as_of=_parse_dt(data.get("as_of")),
        )

    def _parse_holding(self, data: dict[str, Any]) -> Holding:
        return Holding(
            broker=self.flavor,
            symbol=str(data.get("tradingsymbol", data.get("symbol"))).upper(),
            exchange=str(data.get("exchange", "NSE")).upper(),
            quantity=int(data.get("quantity", 0)),
            average_price=data.get("average_price"),
        )

    def _parse_quote(
        self, data: dict[str, Any], *, symbol: str, exchange: str
    ) -> Quote:
        return Quote(
            broker=self.flavor,
            symbol=symbol.upper(),
            exchange=exchange.upper(),
            last_price=float(data["last_price"]),
            bid_price=data.get("bid_price"),
            ask_price=data.get("ask_price"),
            timestamp=_parse_dt(data.get("timestamp")),
        )

    def _parse_order(self, data: dict[str, Any]) -> BrokerOrderRecord:
        raw = str(data.get("status", "")).lower()
        filled = int(data.get("filled_quantity", 0))
        if raw == "open":
            status = OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.PENDING
        else:
            status = self._STATUS_MAP.get(raw)
            if status is None:
                raise BrokerResponseError(f"unknown upstox order status {raw!r}")
        return BrokerOrderRecord(
            broker=self.flavor,
            order_id=str(data["order_id"]),
            tag=data.get("tag"),
            idempotency_key=data.get("idempotency_key"),
            symbol=str(data["symbol"]).upper(),
            exchange=str(data.get("exchange", "NSE")).upper(),
            side=OrderSide(str(data["side"]).upper()),
            quantity=int(data["quantity"]),
            price=float(data["price"]),
            status=status,
            raw_status=raw,
            filled_quantity=filled,
            average_price=data.get("average_price"),
            placed_at=_parse_dt(data.get("placed_at")),
            updated_at=_parse_dt(data.get("updated_at")),
            message=data.get("status_message") or data.get("message"),
            duplicate=bool(data.get("duplicate", False)),
        )

    def _parse_trade(self, data: dict[str, Any]) -> TradeRecord:
        return TradeRecord(
            broker=self.flavor,
            trade_id=str(data["trade_id"]),
            order_id=str(data["order_id"]),
            symbol=str(data.get("tradingsymbol", data.get("symbol"))).upper(),
            exchange=str(data.get("exchange", "NSE")).upper(),
            side=OrderSide(str(data.get("transaction_type", data.get("side"))).upper()),
            quantity=int(data["quantity"]),
            price=float(data.get("average_price", data.get("price"))),
            traded_at=_parse_dt(data.get("exchange_timestamp", data.get("traded_at"))),
        )


class DhanAdapter(BaseSandboxAdapter):
    """Dhan v2 sandbox adapter (mirrors UpstoxAdapter over its own dialect)."""

    flavor = "dhan"

    _STATUS_MAP = {
        "transit": OrderStatus.PENDING,
        "pending": OrderStatus.PENDING,
        "part_traded": OrderStatus.PARTIALLY_FILLED,
        "traded": OrderStatus.FILLED,
        "rejected": OrderStatus.REJECTED,
        "cancelled": OrderStatus.CANCELLED,
        "expired": OrderStatus.EXPIRED,
    }

    def _position_fields(self, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "symbol": item.get("securityId", item.get("symbol")),
            "exchange": item.get("exchange", "NSE"),
            "quantity": item.get("netQty", item.get("quantity")),
            "average_price": item.get("costPrice", item.get("average_price")),
        }

    def _build_place_payload(self, intent: OrderIntent) -> dict[str, Any]:
        return {
            "dhanClientId": f"{self.flavor.upper()}-SANDBOX",
            "correlationId": intent.internal_order_id,
            "tag": intent.internal_order_id,
            "idempotency_key": intent.idempotency_key,
            "transactionType": intent.side.value,
            "side": intent.side.value,
            "exchangeSegment": f"{intent.exchange}_EQ",
            "exchange": intent.exchange,
            "productType": "CNC",
            "orderType": "LIMIT",
            "order_type": "LIMIT",
            "validity": "DAY",
            "tradingSymbol": intent.symbol,
            "symbol": intent.symbol,
            "quantity": intent.quantity,
            "price": intent.limit_price,
        }

    def _parse_profile(self, data: dict[str, Any]) -> BrokerProfile:
        return BrokerProfile(
            broker=self.flavor,
            client_id=str(data.get("dhanClientId") or data.get("client_id")),
            user_name=str(data.get("name", data.get("user_name", "Unknown"))),
            email=data.get("email"),
            exchanges=tuple(data.get("exchanges", ())),
            products=tuple(data.get("products", ())),
        )

    def _parse_funds(self, data: dict[str, Any]) -> FundsSummary:
        available = data.get("availabelBalance", data.get("available_cash", 0.0))
        used = data.get("utilizedAmount", data.get("used_margin", 0.0))
        return FundsSummary(
            broker=self.flavor,
            available_cash=float(available),
            used_margin=float(used),
            currency=str(data.get("currency", "INR")),
            as_of=_parse_dt(data.get("as_of")),
        )

    def _parse_holding(self, data: dict[str, Any]) -> Holding:
        return Holding(
            broker=self.flavor,
            symbol=str(data.get("securityId", data.get("symbol"))).upper(),
            exchange=str(data.get("exchange", "NSE")).upper(),
            quantity=int(data.get("totalQty", data.get("quantity", 0))),
            average_price=data.get("avgCostPrice", data.get("average_price")),
        )

    def _parse_quote(
        self, data: dict[str, Any], *, symbol: str, exchange: str
    ) -> Quote:
        return Quote(
            broker=self.flavor,
            symbol=symbol.upper(),
            exchange=exchange.upper(),
            last_price=float(data["last_price"]),
            bid_price=data.get("bid_price"),
            ask_price=data.get("ask_price"),
            timestamp=_parse_dt(data.get("timestamp")),
        )

    def _parse_order(self, data: dict[str, Any]) -> BrokerOrderRecord:
        raw = str(data.get("orderStatus", data.get("status", ""))).lower()
        status = self._STATUS_MAP.get(raw)
        if status is None:
            raise BrokerResponseError(f"unknown dhan order status {raw!r}")
        return BrokerOrderRecord(
            broker=self.flavor,
            order_id=str(data.get("orderId", data.get("order_id"))),
            tag=data.get("correlationId", data.get("tag")),
            idempotency_key=data.get("idempotencyKey", data.get("idempotency_key")),
            symbol=str(data.get("tradingSymbol", data.get("symbol"))).upper(),
            exchange=str(data.get("exchange", "NSE")).upper(),
            side=OrderSide(str(data.get("transactionType", data.get("side"))).upper()),
            quantity=int(data["quantity"]),
            price=float(data["price"]),
            status=status,
            raw_status=raw,
            filled_quantity=int(data.get("filledQty", data.get("filled_quantity", 0))),
            average_price=data.get("averagePrice", data.get("average_price")),
            placed_at=_parse_dt(data.get("createTime", data.get("placed_at"))),
            updated_at=_parse_dt(data.get("updateTime", data.get("updated_at"))),
            message=data.get("reason", data.get("message")),
            duplicate=bool(data.get("duplicate", False)),
        )

    def _parse_trade(self, data: dict[str, Any]) -> TradeRecord:
        return TradeRecord(
            broker=self.flavor,
            trade_id=str(data.get("tradeId", data.get("trade_id"))),
            order_id=str(data.get("orderId", data.get("order_id"))),
            symbol=str(data.get("tradingSymbol", data.get("symbol"))).upper(),
            exchange=str(data.get("exchange", "NSE")).upper(),
            side=OrderSide(str(data.get("transactionType", data.get("side"))).upper()),
            quantity=int(data.get("tradedQuantity", data.get("quantity"))),
            price=float(data.get("tradedPrice", data.get("price"))),
            traded_at=_parse_dt(data.get("createTime", data.get("traded_at"))),
        )


_BROKER_CLASSES = {
    "upstox": UpstoxAdapter,
    "dhan": DhanAdapter,
}


def create_adapter(broker: str, **kwargs: Any) -> BaseSandboxAdapter:
    """Factory: build the sandbox adapter for one supported broker."""
    name = broker.lower().strip()
    if name not in _BROKER_CLASSES:
        raise BrokerConfigurationError(
            f"unsupported broker {broker!r}; supported: {sorted(_BROKER_CLASSES)}"
        )
    return _BROKER_CLASSES[name](**kwargs)
