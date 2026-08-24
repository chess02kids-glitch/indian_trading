"""API Sessions repository for database synchronization."""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from config.database import get_supabase_client, with_retries
from observability.logging import get_logger

logger = get_logger("quant_india.models.sessions")


class APISessionsRepository:
    """Repository managing the api_sessions table in Supabase."""

    def __init__(self) -> None:
        pass

    @property
    def client(self):
        return get_supabase_client()

    @with_retries(max_retries=3)
    def save_session(
        self,
        user_id: str,
        broker: str,
        access_token: str,
        refresh_token: str,
        expires_at: datetime,
    ) -> Dict[str, Any]:
        """Save or update an API session token for a user."""

        # We attempt to upsert the session for the specific user and broker
        payload = {
            "user_id": user_id,
            "broker": broker,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at.isoformat(),
            "is_active": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        # In a real schema we'd use upsert on a unique constraint (user_id, broker)
        # We query first since we don't know the exact schema constraints
        existing = (
            self.client.table("api_sessions")
            .select("id")
            .eq("user_id", user_id)
            .eq("broker", broker)
            .execute()
        )

        if existing.data:
            session_id = existing.data[0]["id"]
            res = (
                self.client.table("api_sessions")
                .update(payload)
                .eq("id", session_id)
                .execute()
            )
        else:
            payload["id"] = str(uuid.uuid4())
            payload["created_at"] = payload["updated_at"]
            res = self.client.table("api_sessions").insert(payload).execute()

        return res.data[0] if res.data else {}

    @with_retries(max_retries=3)
    def get_active_session(self, user_id: str, broker: str) -> Optional[Dict[str, Any]]:
        """Retrieve the active session for a specific user and broker."""
        res = (
            self.client.table("api_sessions")
            .select("*")
            .eq("user_id", user_id)
            .eq("broker", broker)
            .eq("is_active", True)
            .execute()
        )

        return res.data[0] if res.data else None

    @with_retries(max_retries=3)
    def deactivate_session(self, user_id: str, broker: str) -> None:
        """Mark a session as inactive."""
        self.client.table("api_sessions").update({"is_active": False}).eq(
            "user_id", user_id
        ).eq("broker", broker).execute()
