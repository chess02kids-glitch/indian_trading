from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from models.domain import (
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    Position,
    ReconciliationMismatch,
    ReconciliationResult,
)
from store.supabase import (
    SupabaseOrderRepository,
    SupabasePositionRepository,
    SupabaseReconciliationRepository,
    SupabaseRunRepository,
)


@pytest.fixture
def mock_supabase():
    with patch("store.supabase.get_supabase_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


def test_order_repository_duplicate_order(mock_supabase):
    repo = SupabaseOrderRepository()
    intent = OrderIntent(
        internal_order_id="INT-123",
        idempotency_key="IDK-456",
        strategy_id="S1",
        hypothesis_id="H1",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=2500.0,
        timestamp=datetime.now(timezone.utc),
    )

    # Simulate first insert success
    mock_execute = MagicMock()
    mock_supabase.table.return_value.insert.return_value = mock_execute
    repo.save_intent(intent)

    # Simulate duplicate insert
    class MockAPIError(Exception):
        pass

    mock_supabase.table.return_value.insert.side_effect = MockAPIError(
        "23505 duplicate key"
    )

    # Simulate get_intent returning the existing intent
    mock_get = MagicMock()
    mock_get.execute.return_value.data = [
        {
            "internal_order_id": "INT-123",
            "idempotency_key": "IDK-456",
            "symbol": "RELIANCE",
            "side": "BUY",
            "quantity": 10,
            "price": 2500.0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ]
    mock_supabase.table.return_value.select.return_value.eq.return_value = mock_get

    # It should seamlessly return the existing intent without throwing
    res = repo.save_intent(intent)
    assert res.internal_order_id == "INT-123"


def test_order_repository_fills(mock_supabase):
    repo = SupabaseOrderRepository()
    result = OrderResult(
        internal_order_id="INT-123",
        idempotency_key="IDK-456",
        broker_order_id="BROKER-1",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        status=OrderStatus.PARTIALLY_FILLED,
        requested_quantity=10,
        filled_quantity=5,
        average_fill_price=2500.0,
        timestamp=datetime.now(timezone.utc),
    )

    # Mock finding the DB order ID
    mock_find = MagicMock()
    mock_find.execute.return_value.data = [{"id": "uuid-123"}]
    mock_supabase.table.return_value.select.return_value.eq.return_value = mock_find

    mock_execute = MagicMock()
    mock_supabase.table.return_value.insert.return_value = mock_execute

    repo.save_result(result)

    # Verify executions table insertion
    mock_supabase.table.assert_any_call("executions")
    # Verify status update
    mock_supabase.table.return_value.update.assert_called_with(
        {"status": "PARTIALLY_FILLED"}
    )


def test_position_repository_upsert(mock_supabase):
    repo = SupabasePositionRepository()
    pos = Position(symbol="RELIANCE", quantity=100, average_price=2500.0)

    mock_execute = MagicMock()
    mock_supabase.table.return_value.upsert.return_value = mock_execute

    repo.upsert_position(pos)
    mock_supabase.table.return_value.upsert.assert_called_once()


def test_run_repository_concurrent_claim(mock_supabase):
    repo = SupabaseRunRepository()

    mock_execute = MagicMock()
    mock_supabase.table.return_value.insert.return_value = mock_execute

    # First claim succeeds
    assert repo.claim_run("run-123") is True

    # Concurrent claim fails with duplicate key
    class MockAPIError(Exception):
        pass

    mock_supabase.table.return_value.insert.side_effect = MockAPIError(
        "23505 duplicate key"
    )
    assert repo.claim_run("run-123") is False


def test_reconciliation_repository(mock_supabase):
    repo = SupabaseReconciliationRepository()
    mismatch = ReconciliationMismatch(kind="MISSING_EXECUTION", symbol="RELIANCE")
    result = ReconciliationResult(
        run_id="run-1",
        as_of=datetime.now(timezone.utc),
        matched=False,
        mismatches=(mismatch,),
    )

    mock_execute = MagicMock()
    mock_supabase.table.return_value.upsert.return_value = mock_execute

    repo.save_result(result)
    mock_supabase.table.return_value.upsert.assert_called_once()
