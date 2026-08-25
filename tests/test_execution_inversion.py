import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from execution.service import ExecutionService, ExecutionSummary
from models.domain import PortfolioTarget, OrderResult, OrderSide, OrderStatus, Position
from store.protocols import OrderRepository, PositionRepository
from risk_kill import RiskGuard, RiskState, RiskDecision

class MockMemoryOrderRepository(OrderRepository):
    def __init__(self):
        self.intents = {}
        self.results = {}
        
    def save_intent(self, intent):
        self.intents[intent.internal_order_id] = intent
        return intent
        
    def get_intent(self, internal_order_id):
        return self.intents.get(internal_order_id)
        
    def save_result(self, result):
        self.results[result.internal_order_id] = result
        return result
        
    def get_result(self, internal_order_id):
        return self.results.get(internal_order_id)
        
    def find_by_idempotency_key(self, idempotency_key):
        return next((r for r in self.results.values() if r.idempotency_key == idempotency_key), None)
        
    def list_intents(self):
        return list(self.intents.values())

class MockMemoryPositionRepository(PositionRepository):
    def __init__(self):
        self.positions = {}
        
    def upsert_position(self, position):
        self.positions[position.symbol] = position
        return position
        
    def get_position(self, symbol):
        return self.positions.get(symbol)
        
    def list_positions(self):
        return list(self.positions.values())

class MockBroker:
    def __init__(self):
        self.orders = []
        
    def submit_order(self, intent, reference_price):
        self.orders.append(intent)
        return OrderResult(
            internal_order_id=intent.internal_order_id,
            idempotency_key=intent.idempotency_key,
            broker_order_id="b-" + intent.internal_order_id,
            symbol=intent.symbol,
            side=intent.side,
            status=OrderStatus.PENDING,
            requested_quantity=intent.quantity,
            filled_quantity=0,
            average_fill_price=None,
            timestamp=datetime.now(timezone.utc)
        )
        
    def get_positions(self):
        return []

def test_execution_dependency_inversion():
    """Verify ExecutionService works exactly the same across any compliant repository."""
    target = PortfolioTarget(
        strategy_id="S1",
        hypothesis_id="H1",
        as_of=datetime.now(timezone.utc).date(),
        limits={"RELIANCE": 2500.0},
        target_quantities={"RELIANCE": 10}
    )
    
    mock_risk = MagicMock(spec=RiskGuard)
    mock_risk.evaluate.return_value = RiskDecision(
        state=RiskState.NOMINAL,
        triggered_by=(),
        details={},
        human_action_required=False,
    )
    mock_risk.check_duplicate_order.return_value = None
    
    # 1. Test with Memory Repositories
    mem_orders = MockMemoryOrderRepository()
    mem_positions = MockMemoryPositionRepository()
    broker = MockBroker()
    
    svc_mem = ExecutionService(
        broker=broker,
        order_repository=mem_orders,
        position_repository=mem_positions,
        risk_guard=mock_risk
    )
    
    summary = svc_mem.execute_targets(
        target, 
        run_id="run-1", 
        reference_prices={"RELIANCE": 2490.0},
        risk_context=MagicMock()
    )
    assert len(summary.submitted) == 1
    assert len(mem_orders.intents) == 1
    
    # 2. Test with mock Supabase Repositories
    supa_orders = MagicMock(spec=OrderRepository)
    supa_positions = MagicMock(spec=PositionRepository)
    supa_positions.list_positions.return_value = []
    
    svc_supa = ExecutionService(
        broker=broker,
        order_repository=supa_orders,
        position_repository=supa_positions,
        risk_guard=mock_risk
    )
    
    summary_supa = svc_supa.execute_targets(
        target, 
        run_id="run-2", 
        reference_prices={"RELIANCE": 2490.0},
        risk_context=MagicMock()
    )
    assert len(summary_supa.submitted) == 1
    supa_orders.save_intent.assert_called_once()
    supa_orders.save_result.assert_called_once()
