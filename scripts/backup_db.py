#!/usr/bin/env python3
"""Automated Database Backup utility utilizing pg_dump."""

import os
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from config.logging import setup_logging

logger = logging.getLogger(__name__)

def run_backup(output_dir: Path | str = "./backups") -> None:
    """Executes pg_dump against the configured DATABASE_URL."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL is not set. Cannot run backup.")
        return

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = output_dir / f"supabase_backup_{timestamp}.sql"

    # We use pg_dump to backup the schema and data
    cmd = [
        "pg_dump",
        db_url,
        "--clean",
        "--if-exists",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
        f"--file={backup_file}"
    ]

    try:
        logger.info(f"Starting database backup to {backup_file}")
        # Capture output to avoid exposing DB URL in logs on error
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"Backup completed successfully: {backup_file}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Backup failed. pg_dump returned non-zero exit status.")
        logger.error(f"Error output: {e.stderr}")
    except FileNotFoundError:
        logger.error("pg_dump executable not found. Ensure PostgreSQL client tools are installed.")

if __name__ == "__main__":
    setup_logging()
    run_backup()
