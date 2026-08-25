import logging
import os
from pathlib import Path

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("quant_india.migrations")


def run_migrations() -> None:
    """Executes all SQL migration files in the migrations directory."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL environment variable is required to run migrations.")
        return

    migrations_dir = Path(__file__).parent
    sql_files = sorted(migrations_dir.glob("*.sql"))

    if not sql_files:
        logger.info("No migration files found.")
        return

    conn = None
    try:
        conn = psycopg2.connect(db_url)
        with conn:
            with conn.cursor() as cur:
                # Ensure the tracking table exists
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.schema_migrations (
                        version VARCHAR(255) PRIMARY KEY,
                        applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

                # Fetch applied migrations
                cur.execute("SELECT version FROM public.schema_migrations;")
                applied_migrations = {row[0] for row in cur.fetchall()}

                for sql_file in sql_files:
                    migration_name = sql_file.name
                    if migration_name in applied_migrations:
                        logger.info(
                            f"Skipping already applied migration: {migration_name}"
                        )
                        continue

                    logger.info(f"Applying migration: {migration_name}")
                    with open(sql_file, "r") as f:
                        sql = f.read()

                    try:
                        # Use a savepoint/nested transaction block for each file
                        # Since we are already in `with conn`, everything is one big transaction,
                        # but psycopg2 handles nested blocks cleanly if we just execute.
                        # Wait, the requirement says "fully transactional with schema_migrations".
                        # If a single file fails, the whole transaction rolls back, which is exactly
                        # what `with conn:` provides.
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO public.schema_migrations (version) VALUES (%s);",
                            (migration_name,),
                        )
                        logger.info(f"Successfully applied {migration_name}")
                    except Exception as e:
                        logger.error(f"Failed to apply {migration_name}: {e}")
                        raise
    except Exception as e:
        logger.error(f"Migration runner failed: {e}")
        raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    run_migrations()
