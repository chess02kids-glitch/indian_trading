from typing import Dict, Any

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger

logger = get_logger("quant_india.reconciliation.repositories")


class ReconciliationRepository:
    """Repository for reconciliation logs."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def log_run(self, status: str, discrepancy_details: Dict[str, Any]) -> Dict:
        data = {
            "status": status,
            "discrepancy_details": discrepancy_details,
        }
        res = self.client.table("reconciliation_log").insert(data).execute()
        return res.data[0]
