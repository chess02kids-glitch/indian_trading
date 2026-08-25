#!/usr/bin/env python3
"""Validates and restores an encrypted database backup."""

import argparse
import logging
import os
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

from config.logging import setup_logging

logger = logging.getLogger(__name__)


def restore_backup(backup_file: str) -> None:
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL is not set. Cannot run restore.")
        return

    backup_path = Path(backup_file)
    if not backup_path.exists():
        logger.error(f"Backup file {backup_file} does not exist.")
        return

    logger.info(f"Preparing to restore from {backup_path}")
    raw_sql = None

    # 1. Decrypt if needed
    if backup_path.suffix == ".enc":
        encryption_key = os.getenv("BACKUP_ENCRYPTION_KEY")
        if not encryption_key:
            logger.error("BACKUP_ENCRYPTION_KEY required to decrypt .enc files.")
            return

        fernet = Fernet(encryption_key.encode())
        with open(backup_path, "rb") as f:
            encrypted_data = f.read()

        try:
            raw_sql = fernet.decrypt(encrypted_data)
            logger.info("Successfully decrypted backup file.")
        except Exception as e:
            logger.error(f"Failed to decrypt backup: {e}")
            return
    else:
        with open(backup_path, "rb") as f:
            raw_sql = f.read()

    # 2. Restore using psql
    cmd = ["psql", db_url, "-v", "ON_ERROR_STOP=1"]

    logger.info("Starting database restore via psql...")
    try:
        subprocess.run(cmd, input=raw_sql, capture_output=True, check=True)
        logger.info("Restore completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error("Restore failed. psql returned non-zero exit status.")
        logger.error(f"Error output: {e.stderr.decode('utf-8', errors='ignore')}")


if __name__ == "__main__":
    setup_logging()
    parser = argparse.ArgumentParser(description="Restore DB from backup")
    parser.add_argument(
        "backup_file", help="Path to the backup file (.sql or .sql.enc)"
    )
    args = parser.parse_args()

    restore_backup(args.backup_file)
