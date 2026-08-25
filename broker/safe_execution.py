"""Safe execution layer: ``ExecutionAdapter`` bridge over a sandbox broker.

This is what :class:`execution.service.ExecutionService` (via dependency
injection) or the CLI sandbox flow talks to. The enforcement chain for every
order, in order, *before* anything reaches the broker:

1. **Mode gate** — :func:`broker.mode.check_execution_permitted`; ``LIVE``
   always raises, and this adapter never leaves SANDBOX anyway.
2. **LIMIT-only validation** — :func:`execution.validation.validate_order_intent`
   (MARKET/IOC are unrepresentable and rejected; nothing is converted).
3. **Duplicate prevention** — a repeated idempotency key returns the stored
   result without touching the broker (client-side half; the sandbox backend
   also deduplicates by tag).
4. **Limit-price band** — the limit must be near the reference price.
5. **Rate limiting** — :class:`broker.rate_limit.RateLimiter` (default
   1 order/second), queueing concurrent submissions deterministically.
6. **Token gate** — :meth:`broker.token.TokenManager.get_token` (client-side
   expiry detection) raises :class:`StaleTokenError`, converted here into a
   deterministic ``REJECTED`` result before any network-ish call happens.
7. **Submission with retries** — transient transport errors are retried with
   exponential backoff; exhaustion yields an ``UNKNOWN`` result (the order's
   broker state is genuinely unknown — reconciliation must resolve it, and
   the in-flight idempotency claim prevents blind resubmission).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Mapping

from broker.errors import (
    BrokerAuthenticationError,
    BrokerTransportError,
    StaleTokenError,
)
from broker.interface import BrokerAdapter
from broker.mode import OperatingMode, check_execution_permitted
from broker.models import BrokerOrderRecord
from broker.rate_limit import RateLimiter, call_with_retries
from execution.validation import (
    validate_limit_price_band,
    validate_order_intent,
)
from models.domain import OrderIntent, OrderResult, OrderStatus, Position

__all__ = ["SandboxExecutionAdapter", "DEFAULT_MAX_ATTEMPTS", "DEFAULT_BASE_DELAY"]

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.25

#: Statuses the retry mapper treats as final answers from the broker.
_TERMINAL = (
    OrderStatus.FILLED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.REJECTED,
    OrderStatus.CANCELLED,
    OrderStatus.EXPIRED,
)


class SandboxExecutionAdapter:
    """ExecutionAdapter-protocol bridge to a sandbox broker adapter.

    Satisfies :class:`execution.adapter.ExecutionAdapter`
    (``submit_order``/``cancel_order``/``get_order_status``/``get_positions``/
    ``get_open_orders``) so the existing execution and reconciliation
    pipeline works unchanged — only the adapter instance differs.
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        rate_limiter: RateLimiter | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_delay: float = DEFAULT_BASE_DELAY,
        price_band_fraction: float = 0.10,
    ) -> None:
        # Hard gate: refuse to wrap anything that is not sandbox-mode.
        check_execution_permitted(adapter.mode)
        self._adapter = adapter
        self._rate_limiter = rate_limiter or RateLimiter(1.0, clock=clock, sleep=sleep)
        self._sleep = sleep
        self._max_attempts = int(max_attempts)
        self._base_delay = float(base_delay)
        self._price_band_fraction = float(price_band_fraction)
        # idempotency-key -> result: client-side duplicate prevention.
        self._submitted: dict[str, OrderResult] = {}

    # -- properties -----------------------------------------------------------

    @property
    def mode(self) -> OperatingMode:
        return self._adapter.mode

    @property
    def broker_adapter(self) -> BrokerAdapter:
        return self._adapter

    @property
    def rate_limiter(self) -> RateLimiter:
        return self._rate_limiter

    # -- mapping ---------------------------------------------------------------

    def _to_result(self, record: BrokerOrderRecord, intent: OrderIntent) -> OrderResult:
        return OrderResult.model_validate(
            {
                "internal_order_id": intent.internal_order_id,
                "idempotency_key": intent.idempotency_key,
                "broker_order_id": record.order_id,
                "symbol": record.symbol,
                "side": record.side,
                "status": record.status,
                "requested_quantity": intent.quantity,
                "filled_quantity": record.filled_quantity,
                "average_fill_price": record.average_price,
                "timestamp": record.updated_at
                or record.placed_at
                or datetime.now().astimezone(),
                "reason": record.message,
            }
        )

    def _reject(
        self, intent: OrderIntent, reason: str, *, now: datetime | None = None
    ) -> OrderResult:
        result = OrderResult.model_validate(
            {
                "internal_order_id": intent.internal_order_id,
                "idempotency_key": intent.idempotency_key,
                "broker_order_id": None,
                "symbol": intent.symbol,
                "side": intent.side,
                "status": OrderStatus.REJECTED,
                "requested_quantity": intent.quantity,
                "filled_quantity": 0,
                "average_fill_price": None,
                "timestamp": now or datetime.now().astimezone(),
                "reason": reason,
            }
        )
        self._submitted[intent.idempotency_key] = result
        return result

    def _unknown(self, intent: OrderIntent, reason: str) -> OrderResult:
        """Broker state unknown (timeout exhaustion).

        Deliberately NOT stored for duplicate fast-path reuse by *content*;
        the key IS stored so a retry of the same logical order returns the
        same UNKNOWN result instead of resubmitting blindly.
        """
        result = OrderResult.model_validate(
            {
                "internal_order_id": intent.internal_order_id,
                "idempotency_key": intent.idempotency_key,
                "broker_order_id": None,
                "symbol": intent.symbol,
                "side": intent.side,
                "status": OrderStatus.UNKNOWN,
                "requested_quantity": intent.quantity,
                "filled_quantity": 0,
                "average_fill_price": None,
                "timestamp": datetime.now().astimezone(),
                "reason": reason,
            }
        )
        self._submitted[intent.idempotency_key] = result
        return result

    # -- execution adapter protocol ---------------------------------------------

    def submit_order(self, intent: OrderIntent, reference_price: float) -> OrderResult:
        """Enforce the full safety chain, then submit to the sandbox broker."""
        # 1. mode gate (LIVE/RESEARCH refuse; this adapter is always SANDBOX)
        check_execution_permitted(self.mode)
        # 2. LIMIT-only validation (MARKET/IOC rejected before anything else)
        validated = validate_order_intent(intent)
        # 3. duplicate prevention: same idempotency key -> stored result,
        #    no second broker call, deterministic identical output.
        prior = self._submitted.get(validated.idempotency_key)
        if prior is not None:
            return prior
        # 4. limit-price band (mirrors PaperBroker semantics)
        validate_limit_price_band(
            validated, reference_price, band_fraction=self._price_band_fraction
        )
        # 5. token gate: client-side expiry detection before any submission
        try:
            self._adapter.token_manager.get_token(self._adapter.broker_name)
        except StaleTokenError as exc:
            return self._reject(validated, f"authentication expired: {exc}")
        # 6. rate limiting: paced, queued submission (default 1/second)
        self._rate_limiter.acquire()
        # 7. submission with exponential-backoff retries on transport faults
        try:
            record = call_with_retries(
                lambda: self._adapter.place_limit_order(validated),
                max_attempts=self._max_attempts,
                base_delay=self._base_delay,
                sleep=self._sleep,
                describe=f"place order {validated.internal_order_id}",
            )
        except BrokerAuthenticationError as exc:
            # Broker-side token rejection (e.g. an injected stale token).
            # Never retried blindly; the deterministic outcome is a rejection.
            return self._reject(validated, f"broker rejected authentication: {exc}")
        except BrokerTransportError as exc:
            return self._unknown(
                validated,
                f"broker unreachable after {self._max_attempts} attempt(s): {exc}; "
                "order state unknown — reconcile before any retry",
            )
        result = self._to_result(record, validated)
        self._submitted[validated.idempotency_key] = result
        return result

    def cancel_order(self, internal_order_id: str) -> OrderResult | None:
        """Cancel an open sandbox order (paced through the same limiter)."""
        self._rate_limiter.acquire()
        record = self._adapter.cancel_order(internal_order_id)
        if record is None:
            return None
        intent = OrderIntent.model_validate(
            {
                "internal_order_id": internal_order_id,
                "idempotency_key": record.idempotency_key
                or record.tag
                or internal_order_id,
                "strategy_id": "sandbox",
                "hypothesis_id": "sandbox",
                "symbol": record.symbol,
                "exchange": record.exchange,
                "side": record.side,
                "quantity": record.quantity,
                "limit_price": record.price,
                "timestamp": record.placed_at or datetime.now().astimezone(),
            }
        )
        result = self._to_result(record, intent)
        self._submitted[intent.idempotency_key] = result
        return result

    def get_order_status(self, internal_order_id: str) -> OrderResult | None:
        """Latest broker-side status for an internal order id (or tag).

        Internal ids are not stored broker-side, so the broker reference
        (its order id, or the idempotency tag) is recovered from the
        duplicate-prevention map before querying the broker.
        """
        known = self._local_status(internal_order_id)
        if known is not None:
            ref = known.broker_order_id or known.idempotency_key
            record = self._adapter.get_order_status(ref)
            if record is None:
                return known
            return self._to_result_with_ids(record, known)
        record = self._adapter.get_order_status(internal_order_id)
        if record is None:
            return None
        placeholder = OrderIntent.model_validate(
            {
                "internal_order_id": internal_order_id,
                "idempotency_key": record.idempotency_key
                or record.tag
                or internal_order_id,
                "strategy_id": "sandbox",
                "hypothesis_id": "sandbox",
                "symbol": record.symbol,
                "exchange": record.exchange,
                "side": record.side,
                "quantity": record.quantity,
                "limit_price": record.price,
                "timestamp": record.placed_at or datetime.now().astimezone(),
            }
        )
        return self._to_result(record, placeholder)

    def _local_status(self, internal_order_id: str) -> OrderResult | None:
        for result in self._submitted.values():
            if result.internal_order_id == internal_order_id:
                return result
        return None

    def _to_result_with_ids(
        self, record: BrokerOrderRecord, known: OrderResult
    ) -> OrderResult:
        return OrderResult.model_validate(
            {
                "internal_order_id": known.internal_order_id,
                "idempotency_key": known.idempotency_key,
                "broker_order_id": record.order_id,
                "symbol": record.symbol,
                "side": record.side,
                "status": record.status,
                "requested_quantity": known.requested_quantity,
                "filled_quantity": record.filled_quantity,
                "average_fill_price": record.average_price,
                "timestamp": record.updated_at or record.placed_at or known.timestamp,
                "reason": record.message,
            }
        )

    def get_positions(self) -> list[Position]:
        return self._adapter.get_positions()

    def get_open_orders(self) -> list[OrderResult]:
        """Orders the sandbox broker still considers open."""
        open_records = [
            record
            for record in self._broker_orders()
            if record.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]
        results: list[OrderResult] = []
        for record in open_records:
            known = self._known_for(record)
            if known is not None:
                results.append(self._to_result_with_ids(record, known))
        return results

    def _known_for(self, record: BrokerOrderRecord) -> OrderResult | None:
        for known in self._submitted.values():
            if record.tag and known.internal_order_id == record.tag:
                return known
            if record.idempotency_key and known.idempotency_key == (
                record.idempotency_key
            ):
                return known
            if known.broker_order_id and known.broker_order_id == record.order_id:
                return known
        return None

    def _broker_orders(self) -> list[BrokerOrderRecord]:
        orders: list[BrokerOrderRecord] = []
        for key, result in self._submitted.items():
            if result.status in _TERMINAL and result.broker_order_id is None:
                continue
            record = self._adapter.get_order_status(result.broker_order_id or key)
            if record is not None:
                orders.append(record)
        return orders

    # -- introspection for dashboards --------------------------------------------

    def submitted_results(self) -> Mapping[str, OrderResult]:
        """Snapshot of idempotency-key -> latest known result."""
        return dict(self._submitted)
