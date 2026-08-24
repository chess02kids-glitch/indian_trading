"""OAuth abstraction and concrete implementations for broker authentication."""

import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from auth.secrets import BrokerCredentials, secrets
from observability.logging import get_logger

logger = get_logger("quant_india.auth.oauth")


class OAuthFlow(ABC):
    """Abstract base class for broker OAuth flows."""

    def __init__(self, broker_name: str, credentials: Optional[BrokerCredentials]):
        self.broker_name = broker_name
        self.credentials = credentials

    @property
    def is_configured(self) -> bool:
        return self.credentials is not None

    @abstractmethod
    def generate_login_url(self, state: str) -> str:
        """Generate the OAuth authorization URL."""
        pass

    @abstractmethod
    def trade_code_for_token(self, code: str) -> Dict[str, Any]:
        """Exchange authorization code for access and refresh tokens."""
        pass

    @abstractmethod
    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """Renew the access token using the refresh token."""
        pass


class UpstoxOAuth(OAuthFlow):
    """Upstox API v2 OAuth Implementation."""

    def __init__(self):
        super().__init__("upstox", secrets.config.upstox)
        self.base_url = "https://api.upstox.com/v2/login/authorization"

    def generate_login_url(self, state: str) -> str:
        if not self.is_configured:
            raise ValueError("Upstox credentials not configured.")
        params = {
            "response_type": "code",
            "client_id": self.credentials.api_key,
            "redirect_uri": self.credentials.redirect_uri,
            "state": state,
        }
        return f"{self.base_url}/dialog?{urlencode(params)}"

    def trade_code_for_token(self, code: str) -> Dict[str, Any]:
        if not self.is_configured:
            raise ValueError("Upstox credentials not configured.")

        headers = {
            "accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "code": code,
            "client_id": self.credentials.api_key,
            "client_secret": self.credentials.api_secret,
            "redirect_uri": self.credentials.redirect_uri,
            "grant_type": "authorization_code",
        }

        logger.info(
            f"Trading code for Upstox token (masked client_id={self.credentials.api_key[:4]}...)"
        )
        # In a real scenario we'd do:
        # response = requests.post(f"{self.base_url}/token", headers=headers, data=data)
        # response.raise_for_status()
        # return response.json()

        # MOCK IMPLEMENTATION (since we are preparing infra disconnected from live execution)
        return {
            "access_token": f"upstox_access_{uuid.uuid4().hex[:8]}",
            "refresh_token": f"upstox_refresh_{uuid.uuid4().hex[:8]}",
            "expires_in": 86400,  # 1 day
        }

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        """Upstox API v2 doesn't technically support refresh_token, it requires daily login.
        We mock this to satisfy standard OAuth flows if future changes allow it."""
        logger.info("Upstox doesn't natively support offline refresh. Mocking renewal.")
        return {
            "access_token": f"upstox_access_{uuid.uuid4().hex[:8]}",
            "refresh_token": f"upstox_refresh_{uuid.uuid4().hex[:8]}",
            "expires_in": 86400,
        }


class DhanOAuth(OAuthFlow):
    """Dhan API Auth Implementation."""

    def __init__(self):
        super().__init__("dhan", secrets.config.dhan)
        # Dhan typically uses static JWTs or explicit API tokens for Algo trading.
        # We model this as a seamless standard flow for consistency across the system.
        self.base_url = "https://api.dhan.co/v2"

    def generate_login_url(self, state: str) -> str:
        if not self.is_configured:
            raise ValueError("Dhan credentials not configured.")
        # Mocking a URL for Dhan even if they use static tokens, so our CLI unified flow works.
        params = {
            "client_id": self.credentials.api_key,
            "redirect_uri": self.credentials.redirect_uri,
            "state": state,
        }
        return f"https://auth.dhan.co/login?{urlencode(params)}"

    def trade_code_for_token(self, code: str) -> Dict[str, Any]:
        if not self.is_configured:
            raise ValueError("Dhan credentials not configured.")

        logger.info("Dhan token exchange triggered.")
        return {
            "access_token": f"dhan_access_{uuid.uuid4().hex[:8]}",
            "refresh_token": f"dhan_refresh_{uuid.uuid4().hex[:8]}",
            "expires_in": 2592000,  # 30 days
        }

    def refresh_token(self, refresh_token: str) -> Dict[str, Any]:
        logger.info("Dhan token refresh triggered.")
        return {
            "access_token": f"dhan_access_{uuid.uuid4().hex[:8]}",
            "refresh_token": f"dhan_refresh_{uuid.uuid4().hex[:8]}",
            "expires_in": 2592000,
        }
