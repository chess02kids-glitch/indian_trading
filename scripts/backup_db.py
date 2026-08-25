#!/usr/bin/env python3
"""Automated Database Backup utility utilizing pg_dump."""

import logging
import os

# pg_dump is invoked with a fixed executable and argv list below.
import subprocess  # nosec B404
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
        f"--file={backup_file}",
    ]

    try:
        logger.info(f"Starting database backup to {backup_file}")
        # Capture output to avoid exposing DB URL in logs on error
        # Fixed executable, shell=False, and an argv list prevent shell injection.
        subprocess.run(cmd, capture_output=True, text=True, check=True)

        # 1. Generate checksum of raw SQL
        import hashlib

        with open(backup_file, "rb") as f:
            raw_data = f.read()
        checksum = hashlib.sha256(raw_data).hexdigest()

        # 2. Encrypt the backup
        from cryptography.fernet import Fernet

        encryption_key = os.getenv("BACKUP_ENCRYPTION_KEY")
        if not encryption_key:
            logger.warning(
                "BACKUP_ENCRYPTION_KEY not set! Falling back to unencrypted backup."
            )
        else:
            fernet = Fernet(encryption_key.encode())
            encrypted_data = fernet.encrypt(raw_data)
            encrypted_file = str(backup_file) + ".enc"
            with open(encrypted_file, "wb") as f:
                f.write(encrypted_data)
            # Remove the unencrypted original
            backup_file.unlink()
            backup_file = Path(encrypted_file)

        logger.info(
            f"Backup completed successfully: {backup_file} (SHA256: {checksum})"
        )
    except subprocess.CalledProcessError as e:
        logger.error("Backup failed. pg_dump returned non-zero exit status.")
        logger.error(f"Error output: {e.stderr}")
    except FileNotFoundError:
        logger.error(
            "pg_dump executable not found. Ensure PostgreSQL client tools are installed."
        )


if __name__ == "__main__":
    setup_logging()
    run_backup()
