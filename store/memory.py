"""Thread-safe in-memory repository backends for tests and local paper runs."""

from __future__ import annotations

import threading
from typing import Any, Mapping

from models.domain import (
    OrderIntent,
    OrderResult,
    Position,
    ReconciliationResult,
    ResearchResult,
)

from .protocols import EquitySnapshot

__all__ = [
    "InMemoryEquityRepository",
    "InMemoryOrderRepository",
    "InMemoryPositionRepository",
    "InMemoryReconciliationRepository",
    "InMemoryResearchRepository",
    "InMemoryRunRepository",
]


class InMemoryOrderRepository:
    """Order intents and results held in process memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._intents: dict[str, OrderIntent] = {}
        self._results: dict[str, OrderResult] = {}
        self._by_key: dict[str, str] = {}

    def save_intent(self, intent: OrderIntent) -> OrderIntent:
        with self._lock:
            self._intents[intent.internal_order_id] = intent
            self._by_key.setdefault(intent.idempotency_key, intent.internal_order_id)
            return intent

    def get_intent(self, internal_order_id: str) -> OrderIntent | None:
        with self._lock:
            return self._intents.get(internal_order_id)

    def save_result(self, result: OrderResult) -> OrderResult:
        with self._lock:
            self._results[result.internal_order_id] = result
            return result

    def get_result(self, internal_order_id: str) -> OrderResult | None:
        with self._lock:
            return self._results.get(internal_order_id)

    def find_by_idempotency_key(self, idempotency_key: str) -> OrderResult | None:
        with self._lock:
            order_id = self._by_key.get(idempotency_key)
            if order_id is None:
                return None
            return self._results.get(order_id)

    def list_intents(self) -> list[OrderIntent]:
        with self._lock:
            return list(self._intents.values())


class InMemoryEquityRepository:
    """Equity mark-to-market history held in process memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: list[EquitySnapshot] = []

    def save_snapshot(self, snapshot: EquitySnapshot) -> EquitySnapshot:
        with self._lock:
            self._snapshots.append(snapshot)
            return snapshot

    def snapshot_for_date(self, day: str) -> EquitySnapshot | None:
        with self._lock:
            for snapshot in self._snapshots:
                if snapshot.date == day:
                    return snapshot
        return None

    def history(self, limit: int = 0) -> list[EquitySnapshot]:
        with self._lock:
            rows = list(self._snapshots)
        return rows[-limit:] if limit and limit > 0 else rows


class InMemoryPositionRepository:
    """Positions keyed by (symbol, exchange)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._positions: dict[tuple[str, str], Position] = {}

    def upsert_position(self, position: Position) -> Position:
        with self._lock:
            self._positions[(position.symbol, position.exchange)] = position
            return position

    def get_position(self, symbol: str) -> Position | None:
        with self._lock:
            for (sym, _), position in self._positions.items():
                if sym == symbol:
                    return position
            return None

    def list_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())


class InMemoryRunRepository:
    """Runs with an atomic claim so concurrent executions cannot duplicate a run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._claimed: set[str] = set()

    def claim_run(self, run_id: str, *, resume_awaiting_approval: bool = False) -> bool:
        with self._lock:
            if (
                resume_awaiting_approval
                and self._runs.get(run_id, {}).get("status") == "awaiting_approval"
            ):
                self._runs[run_id]["status"] = "claimed"
                return True
            if not run_id or run_id in self._claimed:
                return False
            self._claimed.add(run_id)
            self._runs[run_id] = {"run_id": run_id, "status": "claimed"}
            return True

    def save_run(
        self, run_id: str, status: str, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._lock:
            record = self._runs.setdefault(run_id, {"run_id": run_id})
            record["status"] = status
            if details is not None:
                record["details"] = dict(details)
            return dict(record)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            return dict(record) if record is not None else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._runs.values()]


class InMemoryResearchRepository:
    """Research results, most recently saved first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: list[ResearchResult] = []

    def save_result(self, result: ResearchResult) -> ResearchResult:
        with self._lock:
            self._results.append(result)
            return result

    def latest_result(self) -> ResearchResult | None:
        with self._lock:
            return self._results[-1] if self._results else None

    def list_by_hypothesis(self, hypothesis_id: str) -> list[ResearchResult]:
        with self._lock:
            return [r for r in self._results if r.hypothesis_id == hypothesis_id]


class InMemoryReconciliationRepository:
    """Reconciliation outcomes, most recently saved first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: list[ReconciliationResult] = []

    def save_result(self, result: ReconciliationResult) -> ReconciliationResult:
        with self._lock:
            self._results.append(result)
            return result

    def latest_result(self, run_id: str | None = None) -> ReconciliationResult | None:
        with self._lock:
            if not self._results:
                return None
            if run_id is None:
                return self._results[-1]
            for result in reversed(self._results):
                if result.run_id == run_id:
                    return result
            return None

    def list_results(self) -> list[ReconciliationResult]:
        with self._lock:
            return list(self._results)
