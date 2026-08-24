"""Session management, expiry detection, and renewal workflows."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from auth.oauth import DhanOAuth, OAuthFlow, UpstoxOAuth
from auth.secrets import secrets
from models.sessions import APISessionsRepository
from observability.logging import get_logger

logger = get_logger("quant_india.auth.session")


class SessionManager:
    """Manages broker sessions, token persistence, and renewals."""

    def __init__(self, user_id: str = "system_user"):
        # By default we use a system_user id for headless VPS algo trading,
        # but it supports multi-tenant user_id mapping.
        self.user_id = user_id
        self.repo = APISessionsRepository()

        self.flows: Dict[str, OAuthFlow] = {
            "upstox": UpstoxOAuth(),
            "dhan": DhanOAuth(),
        }

    def get_flow(self, broker: str) -> OAuthFlow:
        if broker not in self.flows:
            raise ValueError(f"Unsupported broker: {broker}")
        return self.flows[broker]

    def login(self, broker: str, code: str) -> Dict[str, Any]:
        """Complete the login process by trading the code for tokens and storing them."""
        flow = self.get_flow(broker)
        token_data = flow.trade_code_for_token(code)

        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 86400)

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Save to DB
        self.repo.save_session(
            self.user_id, broker, access_token, refresh_token, expires_at
        )

        # Save to local secure storage
        secrets.save_secure_token(broker, token_data)

        logger.info(f"Successfully logged into {broker} and secured session.")
        return token_data

    def get_valid_session(self, broker: str) -> Optional[Dict[str, Any]]:
        """Retrieve a valid session, automatically renewing if expired and possible."""
        # 1. Check local secure storage first for speed
        local_data = secrets.load_secure_token(broker)

        # 2. Check Database for expiry and sync
        db_session = self.repo.get_active_session(self.user_id, broker)

        if not db_session:
            logger.warning(f"No active database session found for {broker}.")
            return None

        expires_at = datetime.fromisoformat(db_session["expires_at"])

        if datetime.now(timezone.utc) > expires_at:
            logger.info(f"Session for {broker} has expired. Attempting renewal...")
            return self._renew_session(broker, db_session["refresh_token"])

        return {
            "access_token": db_session["access_token"],
            "refresh_token": db_session["refresh_token"],
        }

    def _renew_session(
        self, broker: str, refresh_token: str
    ) -> Optional[Dict[str, Any]]:
        """Attempt to renew the session using the refresh token."""
        if not refresh_token:
            logger.error(f"Cannot renew {broker} session: No refresh token available.")
            self.repo.deactivate_session(self.user_id, broker)
            return None

        flow = self.get_flow(broker)
        try:
            token_data = flow.refresh_token(refresh_token)

            access_token = token_data["access_token"]
            new_refresh = token_data.get("refresh_token", refresh_token)
            expires_in = token_data.get("expires_in", 86400)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            self.repo.save_session(
                self.user_id, broker, access_token, new_refresh, expires_at
            )
            secrets.save_secure_token(broker, token_data)

            logger.info(f"Successfully renewed session for {broker}.")
            return token_data

        except Exception as e:
            logger.error(f"Failed to renew {broker} session: {e}")
            self.repo.deactivate_session(self.user_id, broker)
            return None
