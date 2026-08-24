import os
import time
from typing import Any, Callable, Optional, TypeVar

from supabase import Client, create_client

from observability.logging import get_logger

logger = get_logger("quant_india.database")


class DatabaseConfig:
    @property
    def supabase_url(self) -> str:
        url = os.getenv("SUPABASE_URL")
        if not url:
            logger.warning("SUPABASE_URL environment variable is not set.")
        return url or ""

    @property
    def supabase_key(self) -> str:
        key = os.getenv("SUPABASE_KEY")
        if not key:
            logger.warning("SUPABASE_KEY environment variable is not set.")
        return key or ""


config = DatabaseConfig()
_client_instance: Optional[Client] = None


def get_supabase_client() -> Client:
    """Returns a singleton Supabase client."""
    global _client_instance
    if _client_instance is None:
        url = config.supabase_url
        key = config.supabase_key
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set in the environment.")
        _client_instance = create_client(url, key)
    return _client_instance


def check_connection() -> bool:
    """Checks if the Supabase connection is healthy."""
    try:
        client = get_supabase_client()
        # Query the health endpoint or make a simple request to verify access
        client.table("users").select("id").limit(1).execute()
        logger.info("Database connection is healthy.")
        return True
    except Exception as e:
        logger.error(f"Database connection health check failed: {e}")
        return False


T = TypeVar("T")


def with_retries(max_retries: int = 3, backoff_factor: float = 0.5) -> Callable:
    """Decorator to automatically retry database operations on failure."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    logger.warning(
                        f"Database operation '{func.__name__}' failed (attempt {attempt}/{max_retries}): {e}"
                    )
                    time.sleep(backoff_factor * attempt)
            logger.error(f"Database operation '{func.__name__}' failed after {max_retries} attempts.")
            if last_exception:
                raise last_exception
            raise RuntimeError("Unknown error during retries")

        return wrapper

    return decorator
