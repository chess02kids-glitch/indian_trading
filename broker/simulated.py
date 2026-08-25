"""Deterministic simulated broker backend (the "sandbox broker server").

This module models the *broker side* of a sandbox environment: sessions,
funds, holdings/positions, quotes, orders, and trades. It performs no
network I/O; state optionally persists to a JSON file so CLI invocations see
a continuous sandbox account.

Determinism guarantees:

* no wall-clock unless injected (``clock``)
* no randomness — quotes derive from a stable hash of the symbol, and fill
  behaviour is fully scripted by :class:`Fault` instructions
* the same sequence of calls with the same scripted faults always produces
  the same outcome (used by the failure-injection test suite)

Failure injection covers: ``timeout``, ``rejection``, ``partial fill``,
``duplicate request`` (handled idempotently by tag), ``stale token`` and
``disconnect``/reconnect.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from broker.errors import (
    BrokerAuthenticationError,
    BrokerError,
    BrokerRejectedOrderError,
)

__all__ = [
    "Fault",
    "TimeoutFault",
    "DisconnectFault",
    "StaleTokenFault",
    "RejectFault",
    "PartialFillFault",
    "PendingFault",
    "TRANSPORT_FAULTS",
    "BEHAVIOURAL_FAULTS",
    "SimulatedBrokerBackend",
    "default_sandbox_state_path",
]

#: Canonical order statuses used internally by the simulated exchange.
STATUS_OPEN = "open"
STATUS_PARTIAL = "partially"
STATUS_COMPLETE = "complete"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = frozenset(
    {STATUS_COMPLETE, STATUS_REJECTED, STATUS_CANCELLED, STATUS_EXPIRED}
)


def default_sandbox_state_path(
    broker: str, environ: Mapping[str, str] | None = None
) -> Path:
    """Default JSON state file for one broker's sandbox backend."""
    source = os.environ if environ is None else environ
    base = Path(source.get("QUANT_DATA_DIR", "data")) / "broker_sandbox"
    return base / f"{broker.lower()}_sandbox_state.json"


# --------------------------------------------------------------------------
# Failure injection
# --------------------------------------------------------------------------


class Fault:
    """Base class for scripted sandbox failures (consumed one per request)."""

    name = "fault"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.__class__.__name__}()"


class TimeoutFault(Fault):
    """The request times out (no broker-side state change)."""

    name = "timeout"


class DisconnectFault(Fault):
    """The connection drops (the next request reconnects successfully)."""

    name = "disconnect"


class StaleTokenFault(Fault):
    """The broker-side rejects the presented token once (HTTP 401 analogue)."""

    name = "stale_token"


class RejectFault(Fault):
    """The order is created but rejected by the broker with ``reason``."""

    name = "rejection"

    def __init__(self, reason: str = "rejected by broker (injected)") -> None:
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RejectFault(reason={self.reason!r})"


class PartialFillFault(Fault):
    """The order partially fills ``fraction`` of the requested quantity."""

    name = "partial_fill"

    def __init__(self, fraction: float = 0.5) -> None:
        if not 0 < fraction < 1:
            raise BrokerError("partial fill fraction must be in (0, 1)")
        self.fraction = float(fraction)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PartialFillFault(fraction={self.fraction})"


class PendingFault(Fault):
    """The order stays open and fills only after ``polls`` status checks."""

    name = "pending"

    def __init__(self, polls: int = 1) -> None:
        if polls < 1:
            raise BrokerError("pending polls must be >= 1")
        self.polls = int(polls)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PendingFault(polls={self.polls})"


#: Faults that abort the request before any broker-side state change.
TRANSPORT_FAULTS = (TimeoutFault, DisconnectFault)

#: Faults that shape how an accepted order behaves.
BEHAVIOURAL_FAULTS = (RejectFault, PartialFillFault, PendingFault)


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


@dataclass
class _Order:
    """Internal order state (canonical vocabulary)."""

    order_id: str
    tag: str
    idempotency_key: str | None
    symbol: str
    exchange: str
    side: str
    quantity: int
    price: float
    status: str
    filled_quantity: int = 0
    average_price: float | None = None
    message: str | None = None
    placed_at: datetime | None = None
    updated_at: datetime | None = None
    pending_polls: int = 0

    def view(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "tag": self.tag,
            "idempotency_key": self.idempotency_key,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side,
            "order_type": "LIMIT",
            "quantity": self.quantity,
            "price": self.price,
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "average_price": self.average_price,
            "message": self.message,
            "placed_at": _iso(self.placed_at),
            "updated_at": _iso(self.updated_at),
            "duplicate": False,
        }


class SimulatedBrokerBackend:
    """One broker's deterministic sandbox account state and order engine."""

    def __init__(
        self,
        broker: str,
        *,
        state_path: Path | str | None = None,
        initial_cash: float = 1_000_000.0,
        token_ttl_hours: float = 24.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if initial_cash <= 0:
            raise BrokerError("initial_cash must be positive")
        if token_ttl_hours <= 0:
            raise BrokerError("token_ttl_hours must be positive")
        self.broker = broker.lower().strip()
        if not self.broker:
            raise BrokerError("broker name must be non-empty")
        self._state_path = Path(state_path) if state_path else None
        self._initial_cash = float(initial_cash)
        self._token_ttl_hours = float(token_ttl_hours)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cash = float(initial_cash)
        self._positions: dict[str, dict[str, Any]] = {}
        self._orders: dict[str, _Order] = {}
        self._trades: list[dict[str, Any]] = []
        self._sessions: dict[str, datetime] = {}
        self._seq = 0
        self._trade_seq = 0
        self._load()

    # -- clock / persistence ---------------------------------------------

    def _now(self) -> datetime:
        return self._clock()

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrokerError(f"sandbox state unreadable: {exc}") from exc
        if payload.get("broker") != self.broker:
            raise BrokerError(
                f"sandbox state belongs to {payload.get('broker')!r}, not {self.broker!r}"
            )
        self._cash = float(payload.get("cash", self._initial_cash))
        self._positions = {
            key: dict(value) for key, value in payload.get("positions", {}).items()
        }
        self._orders = {}
        for order_id, record in payload.get("orders", {}).items():
            self._orders[order_id] = _Order(
                order_id=str(record["order_id"]),
                tag=str(record["tag"]),
                idempotency_key=record.get("idempotency_key"),
                symbol=str(record["symbol"]),
                exchange=str(record["exchange"]),
                side=str(record["side"]),
                quantity=int(record["quantity"]),
                price=float(record["price"]),
                status=str(record["status"]),
                filled_quantity=int(record.get("filled_quantity", 0)),
                average_price=record.get("average_price"),
                message=record.get("message"),
                placed_at=_parse_dt(record.get("placed_at")),
                updated_at=_parse_dt(record.get("updated_at")),
                pending_polls=int(record.get("pending_polls", 0)),
            )
        self._trades = [dict(t) for t in payload.get("trades", [])]
        self._sessions = {
            token: datetime.fromisoformat(expiry)
            for token, expiry in payload.get("sessions", {}).items()
        }
        self._seq = int(payload.get("seq", len(self._orders)))
        self._trade_seq = int(payload.get("trade_seq", len(self._trades)))

    def _save(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "broker": self.broker,
            "cash": self._cash,
            "positions": self._positions,
            "orders": {
                order_id: {
                    **order.view(),
                    "placed_at": _iso(order.placed_at),
                    "updated_at": _iso(order.updated_at),
                    "pending_polls": order.pending_polls,
                }
                for order_id, order in self._orders.items()
            },
            "trades": list(self._trades),
            "sessions": {
                token: expiry.isoformat() for token, expiry in self._sessions.items()
            },
            "seq": self._seq,
            "trade_seq": self._trade_seq,
            "saved_at": _iso(self._now()),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._state_path.parent),
            prefix=self._state_path.name,
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_name, self._state_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- sessions / authentication -----------------------------------------

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange a manually obtained authorization code for a token."""
        if not isinstance(code, str) or not code.strip():
            raise BrokerAuthenticationError("authorization code is required")
        digest = hashlib.sha256(f"{self.broker}|{code}".encode("utf-8")).hexdigest()
        token = f"{self.broker}-sbtk-{digest[:10]}"
        expires_at = self._now() + timedelta(hours=self._token_ttl_hours)
        self._sessions[token] = expires_at
        self._save()
        return {
            "access_token": token,
            "expires_in": int(self._token_ttl_hours * 3600),
            "expires_at": _iso(expires_at),
        }

    def register_session(self, token: str, expires_at: datetime) -> None:
        """Trust a locally persisted sandbox token (idempotent rehydration)."""
        if not isinstance(token, str) or not token.strip():
            raise BrokerAuthenticationError("token must be a non-empty string")
        existing = self._sessions.get(token)
        if existing is None or existing != expires_at:
            self._sessions[token] = expires_at
            self._save()

    def _require_session(self, token: str | None) -> None:
        if not token:
            raise BrokerAuthenticationError("missing access token")
        expiry = self._sessions.get(token)
        if expiry is None:
            raise BrokerAuthenticationError("unknown access token")
        if self._now() >= expiry:
            raise BrokerAuthenticationError(
                f"access token expired at {expiry.isoformat()}"
            )

    def session_valid(self, token: str | None) -> bool:
        try:
            self._require_session(token)
        except BrokerAuthenticationError:
            return False
        return True

    # -- quotes --------------------------------------------------------------

    def quote(self, symbol: str, exchange: str = "NSE") -> dict[str, Any]:
        """Deterministic quote derived from the symbol (stable across runs)."""
        digest = hashlib.sha256(
            f"{self.broker}|{exchange}|{symbol}".encode("utf-8")
        ).hexdigest()
        base = 50.0 + (int(digest[:8], 16) % 40000) / 10.0
        last = round(base, 2)
        return {
            "symbol": symbol,
            "exchange": exchange,
            "last_price": last,
            "bid_price": round(last * 0.9995, 2),
            "ask_price": round(last * 1.0005, 2),
            "timestamp": _iso(self._now()),
        }

    # -- account snapshots ----------------------------------------------------

    def profile(self) -> dict[str, Any]:
        return {
            "client_id": f"{self.broker.upper()}-SANDBOX",
            "user_name": "Sandbox Operator",
            "email": "sandbox.operator@example.com",
            "exchanges": ["NSE", "BSE"],
            "products": ["CNC", "D"],
        }

    def funds(self) -> dict[str, Any]:
        used = max(0.0, self._initial_cash - self._cash)
        return {
            "available_cash": round(self._cash, 2),
            "used_margin": round(used, 2),
            "currency": "INR",
            "as_of": _iso(self._now()),
        }

    def positions(self) -> list[dict[str, Any]]:
        return [
            dict(position)
            for position in self._positions.values()
            if int(position["quantity"]) != 0
        ]

    holdings = positions

    # -- orders ---------------------------------------------------------------

    def _position_key(self, symbol: str, exchange: str) -> str:
        return f"{exchange}:{symbol}"

    def _apply_fill(self, order: _Order, filled: int, price: float) -> None:
        """Move cash/positions for a fill and record the trade."""
        if filled <= 0:
            return
        key = self._position_key(order.symbol, order.exchange)
        position = self._positions.setdefault(
            key,
            {
                "symbol": order.symbol,
                "exchange": order.exchange,
                "quantity": 0,
                "average_price": None,
            },
        )
        if order.side == "BUY":
            total_cost = (
                int(position["quantity"]) * float(position["average_price"] or 0.0)
                + filled * price
            )
            position["quantity"] = int(position["quantity"]) + filled
            position["average_price"] = round(total_cost / int(position["quantity"]), 4)
            self._cash -= filled * price
        else:
            position["quantity"] = int(position["quantity"]) - filled
            self._cash += filled * price
        self._trade_seq += 1
        self._trades.append(
            {
                "trade_id": f"{self.broker}-trd-{self._trade_seq:06d}",
                "order_id": order.order_id,
                "symbol": order.symbol,
                "exchange": order.exchange,
                "side": order.side,
                "quantity": filled,
                "price": price,
                "traded_at": _iso(self._now()),
            }
        )
        order.filled_quantity += filled
        order.average_price = price
        order.updated_at = self._now()

    def _economics_allow(self, order: _Order, quantity: int) -> str | None:
        """Return a rejection reason when cash/holdings are insufficient."""
        if order.side == "BUY":
            if quantity * order.price > self._cash + 1e-9:
                return "insufficient sandbox funds"
        else:
            position = self._positions.get(
                self._position_key(order.symbol, order.exchange)
            )
            held = int(position["quantity"]) if position else 0
            if quantity > held:
                return "insufficient sandbox holdings"
        return None

    def _find_by_tag(self, tag: str) -> _Order | None:
        for order in self._orders.values():
            if order.tag == tag:
                return order
        return None

    def place_order(
        self,
        payload: Mapping[str, Any],
        *,
        token: str | None,
        behaviour: Fault | None = None,
    ) -> dict[str, Any]:
        """Place one LIMIT order; idempotent on ``tag`` (client order id).

        The tag is the caller's internal order id (the broker-visible client
        idempotency handle). A second placement carrying an already-seen
        ``tag`` returns the *original* order view with ``duplicate=True``
        and never creates a new order — this is the broker-side half of
        duplicate protection (client-side dedup uses the idempotency key).
        """
        self._require_session(token)
        order_type = str(payload.get("order_type", "")).upper()
        if order_type != "LIMIT":
            raise BrokerRejectedOrderError(
                f"sandbox accepts LIMIT orders only, got {order_type!r}"
            )
        tag = str(payload.get("tag") or "")
        if not tag:
            raise BrokerRejectedOrderError("order tag (idempotency key) is required")
        existing = self._find_by_tag(tag)
        if existing is not None:
            view = existing.view()
            view["duplicate"] = True
            view["message"] = "duplicate request: returning original order"
            return view

        symbol = str(payload["symbol"]).upper()
        exchange = str(payload.get("exchange", "NSE")).upper()
        side = str(payload["side"]).upper()
        if side not in ("BUY", "SELL"):
            raise BrokerRejectedOrderError(f"invalid side {side!r}")
        quantity = int(payload["quantity"])
        price = float(payload["price"])
        if quantity <= 0 or price <= 0:
            raise BrokerRejectedOrderError("quantity and price must be positive")

        self._seq += 1
        order = _Order(
            order_id=f"{self.broker}-sbx-{self._seq:08d}",
            tag=tag,
            idempotency_key=(
                str(payload["idempotency_key"])
                if payload.get("idempotency_key")
                else None
            ),
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=quantity,
            price=price,
            status=STATUS_OPEN,
            placed_at=self._now(),
            updated_at=self._now(),
        )
        self._orders[order.order_id] = order

        if isinstance(behaviour, RejectFault):
            order.status = STATUS_REJECTED
            order.message = behaviour.reason
            self._save()
            return order.view()

        reason = self._economics_allow(order, quantity)
        if reason is not None:
            order.status = STATUS_REJECTED
            order.message = reason
            self._save()
            return order.view()

        if isinstance(behaviour, PendingFault):
            order.status = STATUS_OPEN
            order.pending_polls = behaviour.polls
            order.message = "accepted; awaiting fill (sandbox)"
            self._save()
            return order.view()

        if isinstance(behaviour, PartialFillFault):
            filled = max(1, int(quantity * behaviour.fraction))
            self._apply_fill(order, filled, price)
            order.status = STATUS_PARTIAL
            order.message = "partially filled (sandbox)"
            self._save()
            return order.view()

        self._apply_fill(order, quantity, price)
        order.status = STATUS_COMPLETE
        order.message = "filled (sandbox)"
        self._save()
        return order.view()

    def _poll_transition(self, order: _Order) -> None:
        """Advance a PendingFault-scripted order on each status poll."""
        if order.status != STATUS_OPEN or order.pending_polls <= 0:
            return
        order.pending_polls -= 1
        if order.pending_polls > 0:
            order.message = "still open (sandbox)"
            return
        remaining = order.quantity - order.filled_quantity
        reason = self._economics_allow(order, remaining)
        if reason is not None:
            order.status = STATUS_REJECTED
            order.message = reason
        else:
            self._apply_fill(order, remaining, order.price)
            order.status = STATUS_COMPLETE
            order.message = "filled after pending (sandbox)"

    def _resolve(self, ref: str) -> _Order | None:
        order = self._orders.get(ref)
        if order is None:
            order = self._find_by_tag(ref)
        return order

    def get_order(self, ref: str, *, token: str | None) -> dict[str, Any] | None:
        """Latest order view by broker order id or tag (advances pending)."""
        self._require_session(token)
        order = self._resolve(ref)
        if order is None:
            return None
        self._poll_transition(order)
        self._save()
        return order.view()

    def cancel_order(self, ref: str, *, token: str | None) -> dict[str, Any] | None:
        """Cancel an open/partial order; closed orders are returned unchanged."""
        self._require_session(token)
        order = self._resolve(ref)
        if order is None:
            return None
        if order.status in (STATUS_OPEN, STATUS_PARTIAL):
            order.status = STATUS_CANCELLED
            order.message = "cancelled (sandbox)"
            order.updated_at = self._now()
        else:
            order.message = "order already closed; cancel ignored"
        self._save()
        return order.view()

    def list_orders(self, *, token: str | None) -> list[dict[str, Any]]:
        self._require_session(token)
        for order in self._orders.values():
            self._poll_transition(order)
        views = [order.view() for order in self._orders.values()]
        self._save()
        return views

    def trades(self, *, token: str | None) -> list[dict[str, Any]]:
        self._require_session(token)
        return [dict(trade) for trade in self._trades]
