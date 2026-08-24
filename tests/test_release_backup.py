"""Tests for RC-1 backup and migration verification utilities."""

import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.backup import create_backup, run_supabase_hook
from scripts.verify_migrations import verify_migrations


def test_create_backup_archives_duckdb_and_reports(tmp_path):
    database = tmp_path / "market.duckdb"
    database.write_bytes(b"duckdb")
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "daily.html").write_text("report")
    archive = create_backup(
        database,
        reports,
        tmp_path / "backups",
        datetime(2026, 8, 24, tzinfo=timezone.utc),
    )
    with tarfile.open(archive) as bundle:
        assert sorted(bundle.getnames()) == [
            "duckdb/market.duckdb",
            "reports",
            "reports/daily.html",
        ]


def test_create_backup_requires_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(
            tmp_path / "missing.duckdb", tmp_path / "reports", tmp_path / "backups"
        )


def test_run_supabase_hook_is_optional(monkeypatch):
    run_supabase_hook(None)
    calls = []
    monkeypatch.setattr(
        "scripts.backup.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    run_supabase_hook("approved-backup-command")
    assert calls[0][0][0] == ["approved-backup-command"]


def test_verify_migrations_accepts_repository_migrations():
    assert verify_migrations(Path("migrations")) == []
