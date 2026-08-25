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
            raise ValueError(
                "SUPABASE_URL and SUPABASE_KEY must be set in the environment."
            )
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
                    
                    # Detect non-transient errors.
                    # postgrest-py APIError usually has `.code` and `.message`.
                    err_code = getattr(e, "code", "")
                    err_msg = getattr(e, "message", str(e))
                    
                    # 23505 = unique_violation (idempotency error)
                    # 42... = syntax error or access rule violation (RLS)
                    # 23... = integrity constraint violation
                    if str(err_code) == "23505" or "duplicate key value" in err_msg.lower():
                        logger.warning(f"Unique constraint/Idempotency error in {getattr(func, '__name__', '<unknown>')}. Raising immediately: {e}")
                        raise
                    if str(err_code).startswith("42") or str(err_code).startswith("23") or "policy" in err_msg.lower():
                        logger.error(f"Non-transient database error in {getattr(func, '__name__', '<unknown>')}. Raising immediately: {e}")
                        raise

                    logger.warning(
                        "Database operation '%s' failed (attempt %s/%s): %s",
                        getattr(func, "__name__", "<unknown>"),
                        attempt,
                        max_retries,
                        e,
                    )
                    time.sleep(backoff_factor * attempt)
            logger.error(
                f"Database operation '{getattr(func, '__name__', '<unknown>')}' "
                f"failed after {max_retries} attempts."
            )
            if last_exception:
                raise last_exception
            raise RuntimeError("Unknown error during retries")

        return wrapper

    return decorator
