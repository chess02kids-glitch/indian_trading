import os
from pathlib import Path

import psycopg2

from observability.logging import get_logger

logger = get_logger("quant_india.migrations")


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
        with conn:  # Transaction safety block
            with conn.cursor() as cur:
                for sql_file in sql_files:
                    logger.info(f"Applying migration: {sql_file.name}")
                    with open(sql_file, "r") as f:
                        sql = f.read()
                    try:
                        cur.execute(sql)
                        logger.info(f"Successfully applied {sql_file.name}")
                    except Exception as e:
                        logger.error(f"Failed to apply {sql_file.name}: {e}")
                        raise
    except Exception as e:
        logger.error(f"Migration runner failed: {e}")
        raise
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    run_migrations()
