"""Environment validation for deployment safety."""

import os

class ConfigurationError(Exception):
    pass

def validate_environment() -> None:
    """Validate environment variables for safe deployment."""
    system_mode = os.getenv("SYSTEM_MODE", "LOCAL").upper()
    
    if system_mode not in ("LOCAL", "PAPER", "VPS"):
        raise ConfigurationError(f"Invalid SYSTEM_MODE: {system_mode}. Must be LOCAL, PAPER, or VPS.")
        
    db_url = os.getenv("DATABASE_URL")
    if system_mode in ("PAPER", "VPS") and not db_url:
        raise ConfigurationError(f"DATABASE_URL must be set in {system_mode} mode.")

    if system_mode == "LIVE":
        raise ConfigurationError("Live mode is explicitly disabled in this deployment phase.")
        
    if os.getenv("UPSTOX_API_KEY") or os.getenv("DHAN_CLIENT_ID") or os.getenv("UPSTOX_API_SECRET"):
        raise ConfigurationError(
            "Live broker credentials detected in environment. "
            "Refusing to start to prevent accidental live execution in paper/local modes."
        )
