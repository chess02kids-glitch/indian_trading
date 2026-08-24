from typing import Dict, Optional

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger

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
        res = self.client.table("users").insert({"email": email, "role": role}).execute()
        return res.data[0]
