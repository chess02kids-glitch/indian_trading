import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from models.repositories import (
    OrdersRepository,
    OrderAttemptsRepository,
    FillsRepository,
    PositionsRepository,
    ReconciliationRepository
)
from models.domain import OrderIntent, OrderSide, OrderType, OrderResult, OrderStatus, Position, ReconciliationResult, ReconciliationMismatch

@pytest.fixture
def mock_supabase():
    with patch("models.repositories.get_supabase_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client

def test_orders_repository_create(mock_supabase):
    repo = OrdersRepository()
    intent = OrderIntent(
        internal_order_id="INT-123",
        idempotency_key="IDK-456",
        strategy_id="S1",
        hypothesis_id="H1",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=10,
        limit_price=2500.0,
        timestamp=datetime.now(timezone.utc)
    )
    
    mock_execute = MagicMock()
    mock_execute.execute.return_value.data = [{"id": "uuid-123", "status": "PENDING"}]
    mock_supabase.table.return_value.insert.return_value = mock_execute
    
    res = repo.create_order("user-1", intent)
    
    assert res["id"] == "uuid-123"
    mock_supabase.table.assert_called_with("orders")
    mock_supabase.table().insert.assert_called_once()
    args = mock_supabase.table().insert.call_args[0][0]
    assert args["internal_order_id"] == "INT-123"
    assert args["idempotency_key"] == "IDK-456"
    assert args["symbol"] == "RELIANCE"
    assert args["quantity"] == 10

def test_order_attempts_repository(mock_supabase):
    repo = OrderAttemptsRepository()
    mock_execute = MagicMock()
    mock_execute.execute.return_value.data = [{"id": "attempt-1"}]
    mock_supabase.table.return_value.insert.return_value = mock_execute
    
    res = repo.log_attempt("order-1", "idk-1", {"req": "data"}, "PENDING")
    
    assert res["id"] == "attempt-1"
    mock_supabase.table.assert_called_with("order_attempts")
    
def test_fills_repository(mock_supabase):
    repo = FillsRepository()
    result = OrderResult(
        internal_order_id="INT-123",
        idempotency_key="IDK-456",
        broker_order_id="BROKER-789",
        symbol="RELIANCE",
        status=OrderStatus.FILLED,
        requested_quantity=10,
        filled_quantity=10,
        average_fill_price=2500.5,
        timestamp=datetime.now(timezone.utc)
    )
    
    mock_execute = MagicMock()
    mock_execute.execute.return_value.data = [{"id": "fill-1"}]
    mock_supabase.table.return_value.insert.return_value = mock_execute
    
    res = repo.save_fill("order-1", result)
    assert res["id"] == "fill-1"
    mock_supabase.table.assert_called_with("executions")

def test_positions_repository(mock_supabase):
    repo = PositionsRepository()
    pos = Position(symbol="RELIANCE", quantity=100, average_price=2500.0)
    
    mock_execute = MagicMock()
    mock_execute.execute.return_value.data = [{"id": "pos-1"}]
    mock_supabase.table.return_value.upsert.return_value = mock_execute
    
    res = repo.update_position("user-1", pos)
    assert res["id"] == "pos-1"
    mock_supabase.table.assert_called_with("positions")
    
def test_reconciliation_repository(mock_supabase):
    repo = ReconciliationRepository()
    mismatch = ReconciliationMismatch(kind="MISSING_EXECUTION", symbol="RELIANCE")
    result = ReconciliationResult(
        run_id="run-1",
        as_of=datetime.now(timezone.utc),
        matched=False,
        mismatches=(mismatch,)
    )
    
    mock_execute = MagicMock()
    mock_execute.execute.return_value.data = [{"id": "recon-1"}]
    mock_supabase.table.return_value.insert.return_value = mock_execute
    
    res = repo.save_result(result)
    assert res["id"] == "recon-1"
    mock_supabase.table.assert_called_with("reconciliation_log")
