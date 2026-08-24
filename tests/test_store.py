"""Phase 1 tests: repository interface behaviour (in-memory and SQLite)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest

from models.domain import (
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ReconciliationResult,
    ResearchResult,
)
from store import (
    InMemoryOrderRepository,
    InMemoryPositionRepository,
    InMemoryReconciliationRepository,
    InMemoryResearchRepository,
    InMemoryRunRepository,
    SQLiteStore,
)

NOW = datetime(2026, 8, 24, tzinfo=UTC)


def _intent(order_id: str = "ord-1", key: str = "key-1") -> OrderIntent:
    return OrderIntent.model_validate(
        {
            "internal_order_id": order_id,
            "idempotency_key": key,
            "strategy_id": "s",
            "hypothesis_id": "h",
            "symbol": "RELIANCE",
            "side": OrderSide.BUY,
            "quantity": 5,
            "limit_price": 100.0,
            "order_type": OrderType.LIMIT,
            "timestamp": NOW,
        }
    )


def _result(order_id: str = "ord-1", key: str = "key-1") -> OrderResult:
    return OrderResult.model_validate(
        {
            "internal_order_id": order_id,
            "idempotency_key": key,
            "broker_order_id": "b-1",
            "symbol": "RELIANCE",
            "status": OrderStatus.FILLED,
            "requested_quantity": 5,
            "filled_quantity": 5,
            "average_fill_price": 100.0,
            "timestamp": NOW,
        }
    )


class _MemoryBundle:
    def __init__(self) -> None:
        self.orders = InMemoryOrderRepository()
        self.positions = InMemoryPositionRepository()
        self.runs = InMemoryRunRepository()
        self.research = InMemoryResearchRepository()
        self.reconciliation = InMemoryReconciliationRepository()


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return _MemoryBundle()
    return SQLiteStore(tmp_path / "store.sqlite3")


class TestOrderRepository:
    def test_save_and_fetch_intent(self, store) -> None:
        repo = store.orders
        repo.save_intent(_intent())
        assert repo.get_intent("ord-1").symbol == "RELIANCE"
        assert repo.get_intent("missing") is None

    def test_result_and_idempotency_lookup(self, store) -> None:
        repo = store.orders
        repo.save_intent(_intent())
        repo.save_result(_result())
        assert repo.get_result("ord-1").status is OrderStatus.FILLED
        found = repo.find_by_idempotency_key("key-1")
        assert found is not None
        assert found.broker_order_id == "b-1"
        assert repo.find_by_idempotency_key("nope") is None

    def test_list_intents(self, store) -> None:
        repo = store.orders
        repo.save_intent(_intent("ord-1"))
        repo.save_intent(_intent("ord-2", "key-2"))
        assert {i.internal_order_id for i in repo.list_intents()} == {"ord-1", "ord-2"}


class TestPositionRepository:
    def test_upsert_get_list(self, store) -> None:
        repo = store.positions
        repo.upsert_position(Position(symbol="A", quantity=3, average_price=10.0))
        repo.upsert_position(Position(symbol="A", quantity=5, average_price=11.0))
        position = repo.get_position("A")
        assert position is not None
        assert position.quantity == 5
        assert repo.get_position("B") is None
        assert [p.symbol for p in repo.list_positions()] == ["A"]


class TestRunRepository:
    def test_claim_is_exclusive(self, store) -> None:
        repo = store.runs
        assert repo.claim_run("run-1") is True
        assert repo.claim_run("run-1") is False
        assert repo.claim_run("run-2") is True

    def test_concurrent_claim_single_winner(self, store) -> None:
        """Final suite (test 10): concurrent execution cannot duplicate the run."""
        repo = store.runs
        winners: list[bool] = []
        barrier = threading.Barrier(16)

        def worker() -> None:
            barrier.wait()
            winners.append(repo.claim_run("daily-2026-08-24"))

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert winners.count(True) == 1

    def test_save_and_get_run(self, store) -> None:
        repo = store.runs
        repo.claim_run("run-9")
        record = repo.save_run("run-9", "completed", {"health": "HEALTHY"})
        assert record["status"] == "completed"
        assert repo.get_run("run-9")["details"]["health"] == "HEALTHY"
        assert repo.get_run("nope") is None
        assert [r["run_id"] for r in repo.list_runs()] == ["run-9"]


class TestResearchRepository:
    def test_latest_and_by_hypothesis(self, store) -> None:
        repo = store.research

        def result(hyp: str, status: str) -> ResearchResult:
            return ResearchResult.model_validate(
                {
                    "hypothesis_id": hyp,
                    "strategy_id": "momentum",
                    "status": status,
                    "metrics": {"sharpe": 0.5},
                }
            )

        repo.save_result(result("HYP-00001", "accepted"))
        repo.save_result(result("HYP-00002", "rejected"))
        assert repo.latest_result() is not None
        assert repo.latest_result().hypothesis_id == "HYP-00002"
        assert len(repo.list_by_hypothesis("HYP-00001")) == 1

    def test_rejected_results_are_kept(self, store) -> None:
        repo = store.research
        repo.save_result(
            ResearchResult.model_validate(
                {
                    "hypothesis_id": "HYP-00009",
                    "strategy_id": "meanrev",
                    "status": "rejected",
                    "metrics": {"sharpe": -0.2},
                }
            )
        )
        assert repo.latest_result() is not None
        assert repo.latest_result().status == "rejected"


class TestReconciliationRepository:
    def test_latest_and_by_run(self, store) -> None:
        repo = store.reconciliation
        r1 = ReconciliationResult.model_validate(
            {"run_id": "run-1", "as_of": NOW, "matched": True}
        )
        r2 = ReconciliationResult.model_validate(
            {
                "run_id": "run-2",
                "as_of": NOW,
                "matched": False,
                "mismatches": [
                    {
                        "kind": "position_mismatch",
                        "symbol": "A",
                        "expected": 1,
                        "actual": 2,
                    }
                ],
                "locked": True,
            }
        )
        repo.save_result(r1)
        repo.save_result(r2)
        assert repo.latest_result() is not None
        assert repo.latest_result().run_id == "run-2"
        assert repo.latest_result("run-1") is not None
        assert repo.latest_result("run-1").matched is True
        assert repo.latest_result("run-1").locked is False
        assert len(repo.list_results()) == 2
