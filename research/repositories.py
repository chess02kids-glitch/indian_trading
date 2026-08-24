from typing import Any, Dict, Optional

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger

logger = get_logger("quant_india.research.repositories")


class ExperimentsRepository:
    """Repository for experiment storage and MLflow integration."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def save_experiment(
        self,
        hypothesis_id: str,
        validation_outcome: str,
        mlflow_reference: Optional[str] = None,
        commit_hash: Optional[str] = None,
        benchmark_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict:
        data = {
            "hypothesis_id": hypothesis_id,
            "validation_outcome": validation_outcome,
            "mlflow_reference": mlflow_reference,
            "commit_hash": commit_hash,
            "benchmark_summary": benchmark_summary,
        }
        res = self.client.table("experiments").insert(data).execute()
        return res.data[0]
