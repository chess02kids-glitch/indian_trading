from datetime import UTC, datetime

from execution.idempotency import IdempotencyRegistry, compute_idempotency_key
from execution.service import ExecutionService
from models.domain import (
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioTarget,
)
from risk_kill import RiskGuard

# Mock components for isolated testing

class MockBrokerAdapter:
    def __init__(self):
        self.orders = {}
        self.positions = []
        
    def submit_order(self, intent, reference_price):
        from models.domain import OrderResult
        result = OrderResult.model_validate({
            "internal_order_id": intent.internal_order_id,
            "idempotency_key": intent.idempotency_key,
            "broker_order_id": f"broker-{intent.internal_order_id}",
            "symbol": intent.symbol,
            "side": intent.side,
            "status": OrderStatus.FILLED,
            "requested_quantity": intent.quantity,
            "filled_quantity": intent.quantity,
            "average_fill_price": reference_price,
            "timestamp": datetime.now(UTC),
        })
        self.orders[intent.internal_order_id] = result
        return result

    def get_order_status(self, internal_order_id):
        # Return a mock BrokerOrderRecord
        if internal_order_id not in self.orders:
            return None
        from broker.models import BrokerOrderRecord
        res = self.orders[internal_order_id]
        return BrokerOrderRecord(
            broker="mock",
            order_id=res.broker_order_id,
            tag=internal_order_id,
            idempotency_key=res.idempotency_key,
            symbol=res.symbol,
            side=res.side,
            quantity=res.requested_quantity,
            price=res.average_fill_price or 100.0,
            status=res.status,
            filled_quantity=res.filled_quantity,
            average_price=res.average_fill_price
        )
        
    def get_positions(self):
        return self.positions

class MockOrderRepository:
    def __init__(self):
        self.intents = {}
        self.results = {}
        
    def save_intent(self, intent):
        self.intents[intent.internal_order_id] = intent
        return intent
        
    def find_by_idempotency_key(self, key):
        for intent in self.intents.values():
            if getattr(intent, "idempotency_key", None) == key:
                # Return the result if it exists, otherwise intent
                if intent.internal_order_id in self.results:
                    return self.results[intent.internal_order_id]
                
                from models.domain import OrderResult
                return OrderResult.model_validate({
                    "internal_order_id": intent.internal_order_id,
                    "idempotency_key": intent.idempotency_key,
                    "broker_order_id": "",
                    "symbol": intent.symbol,
                    "side": intent.side,
                    "status": OrderStatus.PENDING,
                    "requested_quantity": intent.quantity,
                    "filled_quantity": 0,
                    "average_fill_price": None,
                    "timestamp": datetime.now(UTC)
                })
        return None
        
    def save_result(self, result):
        self.results[result.internal_order_id] = result
        return result

class MockPositionRepository:
    def __init__(self):
        self.positions = {}
    def list_positions(self):
        return list(self.positions.values())
    def get_position(self, symbol):
        return self.positions.get(symbol)
    def upsert_position(self, position):
        self.positions[position.symbol] = position
        return position

class MockIdempotencyRepository:
    def __init__(self):
        self.keys = {}
        
    def claim(self, key):
        if key in self.keys:
            return False
        self.keys[key] = "CLAIMED"
        return True
        
    def is_completed(self, key):
        return self.keys.get(key) == "COMPLETED"
        
    def get_accepted_keys(self):
        return {k: v == "COMPLETED" for k, v in self.keys.items()}
        
    def mark_completed(self, key):
        if key in self.keys:
            self.keys[key] = "COMPLETED"

def test_execution_service_crash_recovery():
    # Setup
    broker = MockBrokerAdapter()
    order_repo = MockOrderRepository()
    pos_repo = MockPositionRepository()
    idem_repo = MockIdempotencyRepository()
    registry = IdempotencyRegistry(repository=idem_repo)
    service = ExecutionService(
        broker=broker,
        order_repository=order_repo,
        position_repository=pos_repo,
        risk_guard=RiskGuard(),
        idempotency_registry=registry
    )
    
    # Simulate an intent that was saved, but execution crashed before completion
    from datetime import date
    target = PortfolioTarget(
        strategy_id="strat-1",
        hypothesis_id="hyp-1",
        as_of=date.today(),
        target_quantities={"RELIANCE": 10},
        limits={"RELIANCE": 2500.0}
    )
    
    # Pre-generate key
    key = compute_idempotency_key({
        "strategy_id": target.strategy_id,
        "hypothesis_id": target.hypothesis_id,
        "symbol": "RELIANCE",
        "side": "BUY",
        "quantity": 10.0,
        "limit_price": 2500.0,
        "order_type": "LIMIT",
        "rebalance_date": target.as_of.isoformat()
    })
    
    # Set up crash state
    idem_repo.keys[key] = "CLAIMED"
    
    # Since it's in flight, the normal flow would skip it.
    # But because it's locally saved in order_repo, it should recover it if broker knows about it.
    # Let's say broker DOES know about it (crashed after submit)
    intent = OrderIntent.model_validate({
        "internal_order_id": "ord-123",
        "idempotency_key": key,
        "strategy_id": "strat-1",
        "hypothesis_id": "hyp-1",
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "side": OrderSide.BUY,
        "quantity": 10,
        "limit_price": 2500.0,
        "order_type": OrderType.LIMIT,
        "timestamp": datetime.now(UTC),
        "target_position": 10
    })
    order_repo.save_intent(intent)
    broker.submit_order(intent, reference_price=2500.0)
    
    # Now run execution
    from risk_kill.guard import RiskContext
    summary = service.execute_targets(
        target,
        run_id="run-1",
        reference_prices={"RELIANCE": 2500.0},
        risk_context=RiskContext(
            now=datetime.now(UTC),
            reconciliation_locked=False,
            gross_exposure=0.0,
            equity_now=10000.0,
            equity_day_start=10000.0,
            equity_peak=10000.0,
            position_exposure={},
            data_last_updated=datetime.now(UTC),
            broker_connected=True,
            order_timestamps=[]
        )
    )
    
    assert len(summary.submitted) == 1
    assert summary.submitted[0].status == OrderStatus.FILLED
    assert summary.submitted[0].internal_order_id == "ord-123"
    
    # Verify idempotency completed
    assert registry._states.get(key) is True
