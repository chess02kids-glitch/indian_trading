from typing import Dict, Optional

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger
from models.domain import OrderIntent, OrderResult, Position, ReconciliationResult

logger = get_logger("quant_india.models.repositories")


class UsersRepository:
    """Repository for user management."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def get_by_id(self, user_id: str) -> Optional[Dict]:
        res = self.client.table("users").select("*").eq("id", user_id).execute()
        return res.data[0] if res.data else None

    @with_retries(max_retries=3)
    def create(self, email: str, role: str = "trader") -> Dict:
        res = (
            self.client.table("users").insert({"email": email, "role": role}).execute()
        )
        return res.data[0]


class OrdersRepository:
    @property
    def client(self):
        return get_supabase_client()
        
    @with_retries(max_retries=3)
    def create_order(self, user_id: str, intent: OrderIntent) -> Dict:
        data = {
            "user_id": user_id,
            "internal_order_id": intent.internal_order_id,
            "idempotency_key": intent.idempotency_key,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "price": intent.limit_price,
            "status": "PENDING"
        }
        res = self.client.table("orders").insert(data).execute()
        return res.data[0]


class OrderAttemptsRepository:
    @property
    def client(self):
        return get_supabase_client()
        
    @with_retries(max_retries=3)
    def log_attempt(self, order_id: str, idempotency_key: str, payload: Dict, status: str, error_message: str = None) -> Dict:
        data = {
            "order_id": order_id,
            "idempotency_key": idempotency_key,
            "request_payload": payload,
            "status": status,
            "error_message": error_message
        }
        res = self.client.table("order_attempts").insert(data).execute()
        return res.data[0]


class FillsRepository:
    @property
    def client(self):
        return get_supabase_client()
        
    @with_retries(max_retries=3)
    def save_fill(self, order_id: str, result: OrderResult) -> Dict:
        data = {
            "order_id": order_id,
            "broker_execution_id": result.broker_order_id,
            "executed_quantity": result.filled_quantity,
            "executed_price": result.average_fill_price or 0.0,
            "execution_time": result.timestamp.isoformat()
        }
        res = self.client.table("executions").insert(data).execute()
        return res.data[0]


class PositionsRepository:
    @property
    def client(self):
        return get_supabase_client()
        
    @with_retries(max_retries=3)
    def update_position(self, user_id: str, position: Position) -> Dict:
        data = {
            "user_id": user_id,
            "symbol": position.symbol,
            "quantity": position.quantity,
            "average_price": position.average_price or 0.0
        }
        res = self.client.table("positions").upsert(data, on_conflict="user_id, symbol").execute()
        return res.data[0]


class ReconciliationRepository:
    @property
    def client(self):
        return get_supabase_client()
        
    @with_retries(max_retries=3)
    def save_result(self, result: ReconciliationResult) -> Dict:
        mismatches = [m.model_dump() for m in result.mismatches]
        data = {
            "run_id": result.run_id,
            "run_timestamp": result.as_of.isoformat(),
            "status": "SUCCESS" if result.matched else "DISCREPANCY_FOUND",
            "matched": result.matched,
            "discrepancy_details": mismatches,
            "locked": result.locked,
            "lock_reason": result.lock_reason
        }
        res = self.client.table("reconciliation_log").insert(data).execute()
        return res.data[0]
