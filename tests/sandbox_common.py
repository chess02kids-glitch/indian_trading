"""Shared builders for broker-sandbox tests (fast, deterministic, hermetic).

Every adapter built here uses:
* a :class:`SimulatedSandboxTransport` whose state file lives in ``tmp_path``
* a :class:`TokenManager` with an injectable clock
* no network, no wall-clock, no real sleeps
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from broker.adapter import BaseSandboxAdapter, create_adapter
from broker.simulated import SimulatedBrokerBackend
from broker.token import FileTokenStore, TokenManager
from broker.transport import SimulatedSandboxTransport
from execution.idempotency import compute_idempotency_key
from models.domain import OrderIntent, OrderSide, OrderType

T0 = datetime(2026, 8, 25, 9, 15, tzinfo=UTC)


class FakeClock:
    """Mutable clock for token/session expiry control."""

    def __init__(self, start: datetime = T0) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, delta: Any) -> None:
        self.moment = self.moment + delta


class FakeMono:
    """Mutable monotonic clock for the rate limiter."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class SleepLog:
    """Fake sleeper that records requested delays without sleeping."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class SandboxEnv:
    """One broker's full sandbox environment (transport+tokens+adapter)."""

    def __init__(
        self,
        root: Path,
        broker: str = "upstox",
        *,
        clock: FakeClock | None = None,
        token_ttl_hours: float = 24.0,
        initial_cash: float = 1_000_000.0,
        state_dir: str = "state",
    ) -> None:
        self.root = Path(root)
        self.broker = broker
        self.clock = clock or FakeClock()
        self.token_store = FileTokenStore(self.root / "tokens")
        self.tokens = TokenManager(self.token_store, clock=self.clock)
        self.state_path = self.root / state_dir / f"{broker}.json"
        self.transport = SimulatedSandboxTransport(
            broker,
            state_path=self.state_path,
            initial_cash=initial_cash,
            token_ttl_hours=token_ttl_hours,
            clock=self.clock,
        )
        self.adapter: BaseSandboxAdapter = create_adapter(
            broker, transport=self.transport, token_manager=self.tokens
        )
        self.sleeps = SleepLog()

    def login(self, code: str = "human-code") -> str:
        """Complete a sandbox login; returns the issued token."""
        record = self.adapter.complete_login(code)
        return record.access_token

    def backend(self) -> SimulatedBrokerBackend:
        return self.transport._backend


def make_intent(
    order_id: str = "ord-1",
    *,
    symbol: str = "RELIANCE",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 10,
    price: float = 100.0,
    strategy: str = "s",
    hypothesis: str = "h",
    rebalance_date: str = "2026-08-25",
    key: str | None = None,
    ts: datetime = T0,
) -> OrderIntent:
    if key is None:
        key = compute_idempotency_key(
            {
                "strategy_id": strategy,
                "hypothesis_id": hypothesis,
                "symbol": symbol,
                "side": side.value,
                "quantity": quantity,
                "limit_price": price,
                "order_type": "limit",
                "rebalance_date": rebalance_date,
            }
        )
    return OrderIntent.model_validate(
        {
            "internal_order_id": order_id,
            "idempotency_key": key,
            "strategy_id": strategy,
            "hypothesis_id": hypothesis,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "limit_price": price,
            "order_type": OrderType.LIMIT,
            "timestamp": ts,
        }
    )
