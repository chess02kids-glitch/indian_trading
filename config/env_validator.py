"""Environment validation for deployment safety."""

import logging
import os

import psycopg2

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


def validate_environment() -> None:
    """Validate environment variables for safe deployment.

    AUDIT-036: ``SYSTEM_MODE`` used to default to ``PAPER`` while
    ``.env.example`` documents ``LOCAL``. ``PAPER`` requires
    ``DATABASE_URL``, so a clean checkout that followed the documented
    setup failed its own preflight. The default is now ``LOCAL`` — the
    least-privileged mode, which reaches neither a remote database nor a
    broker — and an unset mode is logged loudly rather than assumed.
    """
    system_mode = os.getenv("SYSTEM_MODE", "").strip().upper() or "LOCAL"
    if not os.getenv("SYSTEM_MODE"):
        logger.warning(
            "SYSTEM_MODE is not set; assuming LOCAL (no remote database, no "
            "broker). Set SYSTEM_MODE explicitly to PAPER, VPS or PRODUCTION "
            "for any deployment."
        )

    if system_mode not in ("LOCAL", "PAPER", "VPS", "PRODUCTION"):
        raise ConfigurationError(
            f"Invalid SYSTEM_MODE: {system_mode}. Must be LOCAL, PAPER, VPS, or PRODUCTION."
        )

    db_url = os.getenv("DATABASE_URL")
    if system_mode in ("PAPER", "VPS", "PRODUCTION") and not db_url:
        raise ConfigurationError(f"DATABASE_URL must be set in {system_mode} mode.")

    if system_mode == "PRODUCTION":
        if not os.getenv("TELEGRAM_BOT_TOKEN"):
            raise ConfigurationError(
                "TELEGRAM_BOT_TOKEN must be set in PRODUCTION mode."
            )
        if not os.getenv("TELEGRAM_CHAT_ID"):
            raise ConfigurationError("TELEGRAM_CHAT_ID must be set in PRODUCTION mode.")

    if system_mode == "LIVE":
        raise ConfigurationError(
            "Live mode is explicitly disabled in this deployment phase."
        )

    if (
        os.getenv("UPSTOX_API_KEY")
        or os.getenv("DHAN_CLIENT_ID")
        or os.getenv("UPSTOX_API_SECRET")
    ):
        raise ConfigurationError(
            "Live broker credentials detected in environment. "
            "Refusing to start to prevent accidental live execution in paper/local modes."
        )


def validate_database_health() -> None:
    """Validate database connectivity, schema tracking, and RLS policies."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        return  # Local mode or tests without DB

    conn = None
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        # Authentication and DNS are inherently verified by a successful connection.

        # Verify TLS (SSL) is strictly active for production safety.
        if (
            conn.info.ssl_in_use is False
            and "localhost" not in db_url
            and "127.0.0.1" not in db_url
        ):
            raise ConfigurationError("Database connection is NOT using TLS/SSL.")

        with conn.cursor() as cur:
            # 1. Connectivity check (SELECT 1)
            cur.execute("SELECT 1")

            # 2. Schema version check
            cur.execute(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'schema_migrations')"
            )
            if not cur.fetchone()[0]:
                raise ConfigurationError(
                    "Database is missing schema_migrations table. Run migrations first."
                )

            cur.execute("SELECT COUNT(*) FROM schema_migrations")
            migration_count = cur.fetchone()[0]
            if migration_count == 0:
                raise ConfigurationError(
                    "Database has 0 applied migrations. Run migrations first."
                )

            # 3. RLS verification on critical tables
            critical_tables = ["audit_log", "orders", "positions", "executions"]
            for table in critical_tables:
                cur.execute(
                    "SELECT relrowsecurity FROM pg_class WHERE relname = %s", (table,)
                )
                res = cur.fetchone()
                if res is None:
                    # Table might not exist yet if migrations aren't fully applied, but we checked migration_count.
                    pass
                elif not res[0]:
                    raise ConfigurationError(
                        f"CRITICAL SAFETY VIOLATION: Row Level Security is NOT enabled on {table}."
                    )
    except psycopg2.Error as e:
        raise ConfigurationError(f"Database health check failed: {e}")
    finally:
        if conn is not None:
            conn.close()
