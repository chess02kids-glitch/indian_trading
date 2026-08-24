from typing import Dict, Optional

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger

logger = get_logger("quant_india.portfolio.repositories")


class OrdersRepository:
    """Repository for order management."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def create_order(
        self,
        user_id: str,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
    ) -> Dict:
        data = {
            "user_id": user_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": "PENDING",
        }
        res = self.client.table("orders").insert(data).execute()
        return res.data[0]

    @with_retries(max_retries=3)
    def update_status(
        self, order_id: str, status: str, broker_order_id: Optional[str] = None
    ) -> Dict:
        update_data = {"status": status}
        if broker_order_id:
            update_data["broker_order_id"] = broker_order_id
        res = (
            self.client.table("orders").update(update_data).eq("id", order_id).execute()
        )
        return res.data[0] if res.data else {}


class PositionsRepository:
    """Repository for position management."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def get_position(self, user_id: str, symbol: str) -> Optional[Dict]:
        res = (
            self.client.table("positions")
            .select("*")
            .eq("user_id", user_id)
            .eq("symbol", symbol)
            .execute()
        )
        return res.data[0] if res.data else None

    @with_retries(max_retries=3)
    def upsert_position(
        self, user_id: str, symbol: str, quantity: float, average_price: float
    ) -> Dict:
        data = {
            "user_id": user_id,
            "symbol": symbol,
            "quantity": quantity,
            "average_price": average_price,
        }
        # Assuming UNIQUE(user_id, symbol) exists to allow upsert
        res = (
            self.client.table("positions")
            .upsert(data, on_conflict="user_id,symbol")
            .execute()
        )
        return res.data[0]
