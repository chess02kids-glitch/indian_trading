"""Sandbox transports: how adapters reach "the broker".

Two implementations exist:

* :class:`SimulatedSandboxTransport` — the default. Fully in-process,
  deterministic, no sockets, with scripted failure injection. It also shapes
  the backend's canonical views into each broker's wire dialect (Upstox v2
  ``{"status": "success", "data": ...}`` snake-case envelopes; Dhan v2 bare
  camelCase objects and different status strings), so the two adapters face
  genuinely different wire formats.
* :class:`HttpSandboxTransportStub` — the documented seam for a future real
  sandbox HTTP client. It validates that its base URL is a sandbox endpoint
  (anything else raises :class:`LiveTradingDisabledError`) and then refuses
  to perform requests, because no HTTP client is wired into this build.

Base-URL policy: a URL is only acceptable when it is ``simulated://``,
localhost/loopback, or a host starting with ``sandbox.``/``api-sandbox.``.
Anything else is a production endpoint and construction fails closed.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from broker.errors import (
    BrokerAuthenticationError,
    BrokerConfigurationError,
    BrokerResponseError,
    BrokerTransportError,
    LiveTradingDisabledError,
)
from broker.simulated import (
    BEHAVIOURAL_FAULTS,
    TRANSPORT_FAULTS,
    Fault,
    SimulatedBrokerBackend,
    StaleTokenFault,
    default_sandbox_state_path,
)

__all__ = [
    "SANDBOX_ACTIONS",
    "SimulatedSandboxTransport",
    "HttpSandboxTransportStub",
    "validate_sandbox_base_url",
]

#: Logical actions a transport understands.
SANDBOX_ACTIONS = frozenset(
    {
        "ping",
        "token",
        "profile",
        "funds",
        "holdings",
        "positions",
        "quote",
        "place",
        "status",
        "cancel",
        "trades",
    }
)

_SANDBOX_HOST_RE = re.compile(r"^(sandbox\.|api-sandbox\.|.*\.sandbox\.)")


def validate_sandbox_base_url(url: str) -> str:
    """Permit only sandbox base URLs; refuse production endpoints.

    ``simulated://...`` (in-process), loopback HTTP(S), and hosts whose name
    starts with ``sandbox.`` / ``api-sandbox.`` (or a ``.sandbox.`` subdomain)
    are accepted. Anything else raises :class:`LiveTradingDisabledError`, so
    there is no construction path to a live broker.
    """
    if not isinstance(url, str) or not url.strip():
        raise BrokerConfigurationError("base_url must be a non-empty string")
    candidate = url.strip()
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme == "simulated":
        return candidate
    if scheme not in ("http", "https"):
        raise BrokerConfigurationError(
            f"unsupported sandbox URL scheme {scheme!r} in {url!r}"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise BrokerConfigurationError(f"sandbox URL has no host: {url!r}")
    if host in ("localhost", "127.0.0.1", "::1"):
        return candidate
    if _SANDBOX_HOST_RE.match(host):
        return candidate
    raise LiveTradingDisabledError(
        f"refusing non-sandbox broker URL {url!r}: only simulated://, loopback, "
        "and sandbox.* hosts are permitted. Live execution is disabled."
    )


#: Canonical -> wire status vocabularies per flavor.
_UPSTOX_STATUS = {
    "open": "open",
    "partially": "open",  # upstox keeps partial orders "open"
    "complete": "complete",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "expired": "expired",
}
_DHAN_STATUS = {
    "open": "pending",
    "partially": "part_traded",
    "complete": "traded",
    "rejected": "rejected",
    "cancelled": "cancelled",
    "expired": "expired",
}


class SimulatedSandboxTransport:
    """In-process deterministic sandbox network for one broker flavor.

    Fault injection: :meth:`script` queues faults per action (or ``"*"`` for
    every action). Each request consumes at most one queued fault (the
    action's queue first, then the ``"*"`` queue). Transport faults abort the
    request; behavioural faults are handed to the order engine. This makes
    failure sequences deterministic and replayable.
    """

    def __init__(
        self,
        flavor: str,
        backend: SimulatedBrokerBackend | None = None,
        *,
        state_path: Any = None,
        initial_cash: float = 1_000_000.0,
        token_ttl_hours: float = 24.0,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self.flavor = flavor.lower().strip()
        if self.flavor not in ("upstox", "dhan"):
            raise BrokerConfigurationError(
                f"unknown sandbox flavor {flavor!r}; expected 'upstox' or 'dhan'"
            )
        self._backend = backend or SimulatedBrokerBackend(
            self.flavor,
            state_path=(
                state_path
                if state_path is not None
                else default_sandbox_state_path(self.flavor)
            ),
            initial_cash=initial_cash,
            token_ttl_hours=token_ttl_hours,
            clock=clock,
        )
        self._lock = threading.Lock()
        self._scripts: dict[str, list[Fault]] = {}

    # -- fault scripting ---------------------------------------------------

    def script(self, action: str, faults: list[Fault]) -> None:
        """Queue faults for one action (or ``"*"``); consumed FIFO per request."""
        if action != "*" and action not in SANDBOX_ACTIONS:
            raise BrokerConfigurationError(f"unknown action {action!r}")
        with self._lock:
            self._scripts[action] = list(faults)

    def clear_scripts(self) -> None:
        with self._lock:
            self._scripts.clear()

    def _consume_fault(self, action: str) -> Fault | None:
        with self._lock:
            queue = self._scripts.get(action) or self._scripts.get("*") or []
            if not queue:
                return None
            return queue.pop(0)

    # -- authentication ------------------------------------------------------

    def exchange_code(self, code: str) -> dict[str, Any]:
        """Token exchange (authorization code -> access token)."""
        fault = self._consume_fault("token")
        if fault is not None:
            if isinstance(fault, StaleTokenFault):
                raise BrokerAuthenticationError(
                    "token exchange rejected (injected stale token)"
                )
            raise BrokerTransportError(f"token exchange {fault.name} (injected)")
        return self._backend.exchange_code(code)

    def register_session(self, token: str, expires_at: Any) -> None:
        self._backend.register_session(token, expires_at)

    # -- request dispatch -----------------------------------------------------

    def request(
        self,
        action: str,
        *,
        payload: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> Any:
        """Perform one sandbox request and return the wire-shaped response."""
        if action not in SANDBOX_ACTIONS - {"token"}:
            raise BrokerConfigurationError(f"unknown request action {action!r}")
        fault = self._consume_fault(action)
        behaviour: Fault | None = None
        if fault is not None:
            if isinstance(fault, TRANSPORT_FAULTS):
                raise BrokerTransportError(f"{action} request {fault.name} (injected)")
            if isinstance(fault, StaleTokenFault):
                raise BrokerAuthenticationError(
                    "access token rejected by broker (injected stale token)"
                )
            if isinstance(fault, BEHAVIOURAL_FAULTS):
                behaviour = fault
            else:  # pragma: no cover - defensive
                raise BrokerConfigurationError(f"unknown fault {fault!r}")

        if action == "ping":
            return self._wrap({"status": "ok"})
        if action == "profile":
            return self._wrap(self._shape_profile(self._backend.profile()))
        if action == "funds":
            return self._wrap(self._shape_funds(self._backend.funds()))
        if action == "holdings":
            return self._wrap(
                [self._shape_holding(p) for p in self._backend.holdings()]
            )
        if action == "positions":
            return self._wrap(
                [self._shape_position(p) for p in self._backend.positions()]
            )
        if action == "quote":
            params = dict(payload or {})
            quote = self._backend.quote(
                str(params["symbol"]), str(params.get("exchange", "NSE"))
            )
            return self._wrap(quote)
        if action == "place":
            if payload is None:
                raise BrokerResponseError("place requires an order payload")
            view = self._backend.place_order(payload, token=token, behaviour=behaviour)
            return self._wrap(self._shape_order(view))
        if action == "status":
            params = dict(payload or {})
            view = self._backend.get_order(str(params["ref"]), token=token)
            return None if view is None else self._wrap(self._shape_order(view))
        if action == "cancel":
            params = dict(payload or {})
            view = self._backend.cancel_order(str(params["ref"]), token=token)
            return None if view is None else self._wrap(self._shape_order(view))
        if action == "trades":
            return self._wrap(
                [self._shape_trade(t) for t in self._backend.trades(token=token)]
            )
        raise BrokerConfigurationError(
            f"unhandled action {action!r}"
        )  # pragma: no cover

    # -- flavor wire shaping -----------------------------------------------------

    def _wrap(self, data: Any) -> Any:
        """Wrap a shaped payload in the flavor's response envelope."""
        if self.flavor == "upstox":
            return {"status": "success", "data": data}
        return data

    def _shape_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        if self.flavor == "upstox":
            return {
                "user_id": profile["client_id"],
                "user_name": profile["user_name"],
                "email": profile["email"],
                "exchanges": list(profile["exchanges"]),
                "products": list(profile["products"]),
            }
        return {
            "dhanClientId": profile["client_id"],
            "name": profile["user_name"],
            "email": profile["email"],
            "exchanges": list(profile["exchanges"]),
            "products": list(profile["products"]),
        }

    def _shape_funds(self, funds: dict[str, Any]) -> dict[str, Any]:
        if self.flavor == "upstox":
            return {
                "equity": {
                    "available_margin": funds["available_cash"],
                    "used_margin": funds["used_margin"],
                },
                "currency": funds["currency"],
                "as_of": funds["as_of"],
            }
        return {
            "availabelBalance": funds["available_cash"],
            "utilizedAmount": funds["used_margin"],
            "currency": funds["currency"],
            "as_of": funds["as_of"],
        }

    def _shape_holding(self, position: dict[str, Any]) -> dict[str, Any]:
        if self.flavor == "upstox":
            return {
                "tradingsymbol": position["symbol"],
                "exchange": position["exchange"],
                "quantity": position["quantity"],
                "average_price": position["average_price"],
            }
        return {
            "securityId": position["symbol"],
            "exchange": position["exchange"],
            "totalQty": position["quantity"],
            "avgCostPrice": position["average_price"],
        }

    def _shape_position(self, position: dict[str, Any]) -> dict[str, Any]:
        if self.flavor == "upstox":
            return {
                "symbol": position["symbol"],
                "exchange": position["exchange"],
                "quantity": position["quantity"],
                "average_price": position["average_price"],
            }
        return {
            "securityId": position["symbol"],
            "exchange": position["exchange"],
            "netQty": position["quantity"],
            "costPrice": position["average_price"],
        }

    def _shape_order(self, view: dict[str, Any]) -> dict[str, Any]:
        if self.flavor == "upstox":
            return {
                "order_id": view["order_id"],
                "tag": view["tag"],
                "idempotency_key": view.get("idempotency_key"),
                "symbol": view["symbol"],
                "exchange": view["exchange"],
                "side": view["side"],
                "order_type": view["order_type"],
                "quantity": view["quantity"],
                "price": view["price"],
                "status": _UPSTOX_STATUS[view["status"]],
                "filled_quantity": view["filled_quantity"],
                "average_price": view["average_price"],
                "status_message": view["message"],
                "placed_at": view["placed_at"],
                "updated_at": view["updated_at"],
                "duplicate": view["duplicate"],
            }
        return {
            "orderId": view["order_id"],
            "correlationId": view["tag"],
            "idempotencyKey": view.get("idempotency_key"),
            "tradingSymbol": view["symbol"],
            "exchange": view["exchange"],
            "transactionType": view["side"],
            "orderType": view["order_type"],
            "quantity": view["quantity"],
            "price": view["price"],
            "orderStatus": _DHAN_STATUS[view["status"]],
            "filledQty": view["filled_quantity"],
            "averagePrice": view["average_price"],
            "reason": view["message"],
            "createTime": view["placed_at"],
            "updateTime": view["updated_at"],
            "duplicate": view["duplicate"],
        }

    def _shape_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        if self.flavor == "upstox":
            return {
                "trade_id": trade["trade_id"],
                "order_id": trade["order_id"],
                "tradingsymbol": trade["symbol"],
                "exchange": trade["exchange"],
                "transaction_type": trade["side"],
                "quantity": trade["quantity"],
                "average_price": trade["price"],
                "exchange_timestamp": trade["traded_at"],
            }
        return {
            "tradeId": trade["trade_id"],
            "orderId": trade["order_id"],
            "tradingSymbol": trade["symbol"],
            "exchange": trade["exchange"],
            "transactionType": trade["side"],
            "tradedQuantity": trade["quantity"],
            "tradedPrice": trade["price"],
            "createTime": trade["traded_at"],
        }


class HttpSandboxTransportStub:
    """Documented seam for a real sandbox HTTP client (deliberately inert).

    Construction validates the base URL via :func:`validate_sandbox_base_url`
    — a production URL fails construction with
    :class:`LiveTradingDisabledError`. Requests always raise
    :class:`BrokerTransportError` because this build ships no HTTP client;
    the simulated transport is the only functional one by design.
    """

    def __init__(self, base_url: str) -> None:
        self.base_url = validate_sandbox_base_url(base_url)
        parsed = urlparse(self.base_url)
        if parsed.scheme == "simulated":
            raise BrokerConfigurationError(
                "simulated:// URLs are served by SimulatedSandboxTransport, "
                "not the HTTP stub"
            )

    def request(self, action: str, **kwargs: Any) -> Any:
        raise BrokerTransportError(
            "offline build: no HTTP client is wired into the sandbox layer; "
            "use SimulatedSandboxTransport"
        )

    def exchange_code(self, code: str) -> Any:
        raise BrokerTransportError(
            "offline build: no HTTP client is wired into the sandbox layer; "
            "use SimulatedSandboxTransport"
        )
