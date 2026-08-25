"""Supabase-backed implementations of the core persistence protocols."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from config.database import get_supabase_client, with_retries
from models.domain import (
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    ReconciliationMismatch,
    ReconciliationResult,
    ResearchResult,
)

logger = logging.getLogger(__name__)

# Hardcoded system user for headless execution until multi-tenant is required.
# In a real system, this would be fetched from auth context or config.
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"


class SupabaseOrderRepository:
    def __init__(self, user_id: str = SYSTEM_USER_ID) -> None:
        self.user_id = user_id

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def save_intent(self, intent: OrderIntent) -> OrderIntent:
        data = {
            "user_id": self.user_id,
            "internal_order_id": intent.internal_order_id,
            "idempotency_key": intent.idempotency_key,
            "symbol": intent.symbol,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "price": intent.limit_price,
            "status": "PENDING"
        }
        try:
            self.client.table("orders").insert(data).execute()
        except Exception as e:
            err_str = str(e)
            if "23505" in err_str or "duplicate key" in err_str:
                # Idempotency constraint violated. 
                # Another process already saved this intent. We fetch and return it,
                # effectively masking the duplicate write as a successful idempotent save.
                return self.get_intent(intent.internal_order_id) or intent
            raise
        return intent

    @with_retries(max_retries=3)
    def get_intent(self, internal_order_id: str) -> OrderIntent | None:
        res = self.client.table("orders").select("*").eq("internal_order_id", internal_order_id).execute()
        if not res.data:
            return None
        row = res.data[0]
        return OrderIntent.model_validate({
            "internal_order_id": row["internal_order_id"],
            "idempotency_key": row["idempotency_key"],
            "strategy_id": "unknown", # Not persisted in orders currently
            "hypothesis_id": "unknown",
            "symbol": row["symbol"],
            "exchange": "NSE",
            "side": row["side"],
            "quantity": int(row["quantity"]),
            "limit_price": float(row["price"]),
            "order_type": OrderType.LIMIT,
            "timestamp": row["created_at"]
        })

    @with_retries(max_retries=3)
    def save_result(self, result: OrderResult) -> OrderResult:
        # First get the order internal ID to find the DB order UUID
        order_res = self.client.table("orders").select("id").eq("internal_order_id", result.internal_order_id).execute()
        if not order_res.data:
            raise ValueError(f"Cannot save result for unknown intent: {result.internal_order_id}")
        
        db_order_id = order_res.data[0]["id"]
        
        # Update order status
        self.client.table("orders").update({"status": result.status.value}).eq("id", db_order_id).execute()

        # If it's a fill, save to executions
        if result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and result.filled_quantity > 0:
            fill_data = {
                "order_id": db_order_id,
                "broker_execution_id": result.broker_order_id or f"fill-{result.internal_order_id}",
                "executed_quantity": result.filled_quantity,
                "executed_price": result.average_fill_price or 0.0,
                "execution_time": result.timestamp.isoformat()
            }
            try:
                self.client.table("executions").insert(fill_data).execute()
            except Exception as e:
                err_str = str(e)
                if "23505" not in err_str and "duplicate key" not in err_str:
                    raise

        return result

    @with_retries(max_retries=3)
    def get_result(self, internal_order_id: str) -> OrderResult | None:
        # Simplistic read: relies on orders table state
        order_res = self.client.table("orders").select("*").eq("internal_order_id", internal_order_id).execute()
        if not order_res.data:
            return None
        row = order_res.data[0]
        
        # Read executions to determine filled quantity
        db_order_id = row["id"]
        exec_res = self.client.table("executions").select("*").eq("order_id", db_order_id).execute()
        
        filled_qty = sum(int(e["executed_quantity"]) for e in exec_res.data)
        avg_price = None
        if filled_qty > 0:
            avg_price = sum(float(e["executed_price"]) * int(e["executed_quantity"]) for e in exec_res.data) / filled_qty

        return OrderResult.model_validate({
            "internal_order_id": row["internal_order_id"],
            "idempotency_key": row["idempotency_key"],
            "broker_order_id": row.get("broker_order_id"),
            "symbol": row["symbol"],
            "side": row["side"],
            "status": row["status"],
            "requested_quantity": int(row["quantity"]),
            "filled_quantity": filled_qty,
            "average_fill_price": avg_price,
            "timestamp": row["updated_at"]
        })

    @with_retries(max_retries=3)
    def find_by_idempotency_key(self, idempotency_key: str) -> OrderResult | None:
        res = self.client.table("orders").select("internal_order_id").eq("idempotency_key", idempotency_key).execute()
        if not res.data:
            return None
        return self.get_result(res.data[0]["internal_order_id"])

    def list_intents(self) -> list[OrderIntent]:
        # Typically not needed in live system
        return []


class SupabasePositionRepository:
    def __init__(self, user_id: str = SYSTEM_USER_ID) -> None:
        self.user_id = user_id

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def upsert_position(self, position: Position) -> Position:
        data = {
            "user_id": self.user_id,
            "symbol": position.symbol,
            "quantity": position.quantity,
            "average_price": position.average_price or 0.0
        }
        self.client.table("positions").upsert(data, on_conflict="user_id, symbol").execute()
        return position

    @with_retries(max_retries=3)
    def get_position(self, symbol: str) -> Position | None:
        res = self.client.table("positions").select("*").eq("user_id", self.user_id).eq("symbol", symbol).execute()
        if not res.data:
            return None
        row = res.data[0]
        return Position.model_validate({
            "symbol": row["symbol"],
            "exchange": "NSE",
            "quantity": int(row["quantity"]),
            "average_price": float(row["average_price"]),
            "updated_at": row["updated_at"]
        })

    @with_retries(max_retries=3)
    def list_positions(self) -> list[Position]:
        res = self.client.table("positions").select("*").eq("user_id", self.user_id).execute()
        return [
            Position.model_validate({
                "symbol": row["symbol"],
                "exchange": "NSE",
                "quantity": int(row["quantity"]),
                "average_price": float(row["average_price"]),
                "updated_at": row["updated_at"]
            })
            for row in res.data
        ]


class SupabaseRunRepository:
    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def claim_run(self, run_id: str) -> bool:
        # We use reconciliation_log's run_id unique constraint to atomically claim runs
        data = {
            "run_id": run_id,
            "status": "CLAIMED",
            "matched": False
        }
        try:
            self.client.table("reconciliation_log").insert(data).execute()
            return True
        except Exception as e:
            err_str = str(e)
            if "23505" in err_str or "duplicate key" in err_str:
                return False
            raise

    @with_retries(max_retries=3)
    def save_run(
        self, run_id: str, status: str, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.client.table("reconciliation_log").update({
            "status": status,
            "discrepancy_details": dict(details) if details else None
        }).eq("run_id", run_id).execute()
        return {"run_id": run_id, "status": status}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        res = self.client.table("reconciliation_log").select("*").eq("run_id", run_id).execute()
        return res.data[0] if res.data else None

    def list_runs(self) -> list[dict[str, Any]]:
        res = self.client.table("reconciliation_log").select("*").execute()
        return res.data


class SupabaseResearchRepository:
    # Minimal implementation if needed by Agent 1
    def save_result(self, result: ResearchResult) -> ResearchResult:
        return result

    def latest_result(self) -> ResearchResult | None:
        return None

    def list_by_hypothesis(self, hypothesis_id: str) -> list[ResearchResult]:
        return []


class SupabaseReconciliationRepository:
    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def save_result(self, result: ReconciliationResult) -> ReconciliationResult:
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
        self.client.table("reconciliation_log").upsert(data, on_conflict="run_id").execute()
        return result

    @with_retries(max_retries=3)
    def latest_result(self, run_id: str | None = None) -> ReconciliationResult | None:
        query = self.client.table("reconciliation_log").select("*")
        if run_id:
            res = query.eq("run_id", run_id).execute()
        else:
            res = query.order("created_at", desc=True).limit(1).execute()
            
        if not res.data:
            return None
        row = res.data[0]
        return ReconciliationResult.model_validate({
            "run_id": row["run_id"],
            "as_of": row["run_timestamp"],
            "matched": bool(row["matched"]),
            "mismatches": row.get("discrepancy_details") or [],
            "locked": bool(row.get("locked")),
            "lock_reason": row.get("lock_reason"),
            "resolved_by": row.get("resolved_by"),
            "resolved_at": row.get("resolved_at"),
        })

    def list_results(self) -> list[ReconciliationResult]:
        return []
