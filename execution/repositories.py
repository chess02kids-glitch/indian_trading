import datetime
from typing import Dict, List

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger

logger = get_logger("quant_india.execution.repositories")


class ExecutionsRepository:
    """Repository for execution management."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def record_execution(
        self,
        order_id: str,
        executed_quantity: float,
        executed_price: float,
        execution_time: datetime.datetime,
        broker_execution_id: str,
    ) -> Dict:
        data = {
            "order_id": order_id,
            "executed_quantity": executed_quantity,
            "executed_price": executed_price,
            "execution_time": execution_time.isoformat(),
            "broker_execution_id": broker_execution_id,
        }
        res = self.client.table("executions").insert(data).execute()
        return res.data[0]

    @with_retries(max_retries=3)
    def get_by_order(self, order_id: str) -> List[Dict]:
        res = self.client.table("executions").select("*").eq("order_id", order_id).execute()
        return res.data
