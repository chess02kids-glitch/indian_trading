"""Secrets management, environment loading, and encryption hooks for authentication."""

import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from observability.logging import get_logger

logger = get_logger("quant_india.auth.secrets")


@dataclass
class BrokerCredentials:
    """Credentials configuration for a specific broker."""

    api_key: str
    api_secret: str
    redirect_uri: str


@dataclass
class AuthConfiguration:
    """Typed configuration for authentication systems."""

    encryption_key_base: str = field(
        default_factory=lambda: os.getenv("QUANT_ENCRYPTION_KEY", "")
    )
    whitelisted_ips: List[str] = field(
        default_factory=lambda: [
            ip.strip()
            for ip in os.getenv("QUANT_WHITELISTED_IPS", "").split(",")
            if ip.strip()
        ]
    )
    upstox: Optional[BrokerCredentials] = None
    dhan: Optional[BrokerCredentials] = None
    storage_dir: Path = field(
        default_factory=lambda: Path(os.getenv("QUANT_DATA_DIR", "data")) / "sessions"
    )

    def __post_init__(self):
        self.storage_dir.mkdir(parents=True, exist_ok=True)


class SecretManager:
    """Handles environment loading, validation, and encrypted storage."""

    def __init__(self, config: Optional[AuthConfiguration] = None):
        self.config = config or self._load_from_env()
        self._fernet = self._initialize_encryption()

    def _load_from_env(self) -> AuthConfiguration:
        """Load and validate secrets from the environment."""
        config = AuthConfiguration()

        upstox_key = os.getenv("UPSTOX_API_KEY")
        upstox_secret = os.getenv("UPSTOX_API_SECRET")
        upstox_redirect = os.getenv("UPSTOX_REDIRECT_URI", "http://localhost:8080")

        if upstox_key and upstox_secret:
            config.upstox = BrokerCredentials(
                api_key=upstox_key,
                api_secret=upstox_secret,
                redirect_uri=upstox_redirect,
            )

        dhan_key = os.getenv("DHAN_CLIENT_ID")
        dhan_secret = os.getenv("DHAN_API_SECRET")
        dhan_redirect = os.getenv("DHAN_REDIRECT_URI", "http://localhost:8080")

        if dhan_key and dhan_secret:
            config.dhan = BrokerCredentials(
                api_key=dhan_key, api_secret=dhan_secret, redirect_uri=dhan_redirect
            )

        return config

    def _initialize_encryption(self) -> Optional[Fernet]:
        """Derive a secure encryption key from the environment base key."""
        base_key = self.config.encryption_key_base
        if not base_key:
            logger.warning(
                "QUANT_ENCRYPTION_KEY not set. Local session storage will be unencrypted."
            )
            return None

        # Derive a 32-byte url-safe base64-encoded key
        salt = b"quant_india_auth_salt"
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(base_key.encode()))
        return Fernet(key)

    def verify_startup(self) -> bool:
        """Validate that critical secrets exist for deployment."""
        if not self.config.encryption_key_base:
            logger.error("Startup validation failed: QUANT_ENCRYPTION_KEY is missing.")
            return False
        if not self.config.upstox and not self.config.dhan:
            logger.warning("No broker credentials loaded.")
        return True

    def validate_ip(self, ip_address: str) -> bool:
        """Whitelist validation for static IP management."""
        if not self.config.whitelisted_ips:
            return True  # If no whitelist is set, allow all (or reject all based on policy, but allow for now)
        return ip_address in self.config.whitelisted_ips

    def save_secure_token(self, broker: str, payload: Dict[str, str]) -> None:
        """Store a token payload securely to local storage."""
        data = json.dumps(payload).encode()
        if self._fernet:
            data = self._fernet.encrypt(data)

        filepath = self.config.storage_dir / f"{broker}_session.dat"
        filepath.write_bytes(data)

    def load_secure_token(self, broker: str) -> Optional[Dict[str, str]]:
        """Load and decrypt a token payload from local storage."""
        filepath = self.config.storage_dir / f"{broker}_session.dat"
        if not filepath.exists():
            return None

        data = filepath.read_bytes()
        if self._fernet:
            try:
                data = self._fernet.decrypt(data)
            except Exception as e:
                logger.error(f"Failed to decrypt token for {broker}: {e}")
                return None

        try:
            return json.loads(data.decode())
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON in token payload for {broker}")
            return None


# Global secret manager instance
secrets = SecretManager()
