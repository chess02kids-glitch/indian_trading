"""Create timestamped, restorable DuckDB and report archives.

This utility never uploads data or invokes a Supabase API itself.  A separately
managed ``SUPABASE_BACKUP_COMMAND`` hook may be supplied by the operator for
an approved Supabase backup workflow.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path


def create_backup(
    duckdb_path: Path, reports_dir: Path, destination: Path, now: datetime | None = None
) -> Path:
    """Archive the DuckDB file and reports directory into a timestamped tar.gz.

    The caller must schedule this outside write-heavy workloads or arrange a
    DuckDB checkpoint first; this avoids pretending a raw file copy is a
    transactionally coordinated database backup.
    """
    if not duckdb_path.is_file():
        raise FileNotFoundError(f"DuckDB database does not exist: {duckdb_path}")
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"quant-india-{timestamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(duckdb_path, arcname=f"duckdb/{duckdb_path.name}")
        if reports_dir.is_dir():
            output.add(reports_dir, arcname="reports")
    return archive


def run_supabase_hook(command: str | None) -> None:
    """Run the operator-approved Supabase backup hook, if explicitly configured."""
    if command:
        subprocess.run(shlex.split(command), check=True)


def main(argv: list[str] | None = None) -> int:
    """Run local backup and optional Supabase hook from the command line."""
    parser = argparse.ArgumentParser(description="Create a Quant India backup archive")
    parser.add_argument(
        "--duckdb", default=os.getenv("DUCKDB_PATH", "data/quant_india.duckdb")
    )
    parser.add_argument("--reports", default="reports")
    parser.add_argument("--destination", default=os.getenv("BACKUP_DIR", "backups"))
    args = parser.parse_args(argv)
    archive = create_backup(
        Path(args.duckdb), Path(args.reports), Path(args.destination)
    )
    run_supabase_hook(os.getenv("SUPABASE_BACKUP_COMMAND"))
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
