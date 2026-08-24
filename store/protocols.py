"""Repository interfaces between the core system and persistence.

The core application depends only on these protocols. Today the backends are
in-memory and SQLite; Agent 2 implements Supabase adapters against the same
interfaces without touching domain logic.

Design rules:

* No Supabase/Postgres imports in this package.
* Repositories store immutable domain objects (``models.domain``) or plain
  JSON-serializable dicts.
* ``RunRepository.claim_run`` must be concurrency-safe: for a given run id,
  exactly one caller may win the claim, so concurrent executions cannot
  duplicate a run.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable

from models.domain import (
    OrderIntent,
    OrderResult,
    Position,
    ReconciliationResult,
    ResearchResult,
)

__all__ = [
    "OrderRepository",
    "PositionRepository",
    "ReconciliationRepository",
    "ResearchRepository",
    "RunRepository",
]


@runtime_checkable
class OrderRepository(Protocol):
    """Persistence for order intents and their execution results."""

    def save_intent(self, intent: OrderIntent) -> OrderIntent: ...

    def get_intent(self, internal_order_id: str) -> OrderIntent | None: ...

    def save_result(self, result: OrderResult) -> OrderResult: ...

    def get_result(self, internal_order_id: str) -> OrderResult | None: ...

    def find_by_idempotency_key(self, idempotency_key: str) -> OrderResult | None: ...

    def list_intents(self) -> list[OrderIntent]: ...


@runtime_checkable
class PositionRepository(Protocol):
    """Persistence for current positions."""

    def upsert_position(self, position: Position) -> Position: ...

    def get_position(self, symbol: str) -> Position | None: ...

    def list_positions(self) -> list[Position]: ...


@runtime_checkable
class RunRepository(Protocol):
    """Persistence for orchestration runs with an atomic claim primitive."""

    def claim_run(self, run_id: str) -> bool:
        """Atomically claim a run id. Returns True for exactly one caller."""
        ...

    def save_run(
        self, run_id: str, status: str, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def list_runs(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class ResearchRepository(Protocol):
    """Persistence for research results (incl. rejected experiments)."""

    def save_result(self, result: ResearchResult) -> ResearchResult: ...

    def latest_result(self) -> ResearchResult | None: ...

    def list_by_hypothesis(self, hypothesis_id: str) -> list[ResearchResult]: ...


@runtime_checkable
class ReconciliationRepository(Protocol):
    """Persistence for reconciliation outcomes."""

    def save_result(self, result: ReconciliationResult) -> ReconciliationResult: ...

    def latest_result(
        self, run_id: str | None = None
    ) -> ReconciliationResult | None: ...

    def list_results(self) -> list[ReconciliationResult]: ...
