#!/usr/bin/env python3
"""Deployment pre-flight: refuse to start an unsafe configuration.

AUDIT-020: :func:`config.env_validator.validate_environment` implements the
repository's own deployment-safety policy (SYSTEM_MODE allow-list, DATABASE_URL
required outside LOCAL, live-broker credentials rejected, Telegram alerting
required in PRODUCTION) — but *nothing ever called it*. The only consumers were
``tests/test_observability.py``. A deployment therefore skipped every one of
those checks and started anyway.

Run this before starting any long-lived process::

    python scripts/preflight.py          # env checks only
    python scripts/preflight.py --db     # env + database/RLS checks
    python scripts/preflight.py --json   # machine-readable

It exits non-zero on the first violation, which is the point: deployment must
fail closed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _checks() -> list[tuple[str, bool, str]]:
    """Return ``(name, ok, detail)`` for every environment check."""
    from config.env_validator import validate_environment

    try:
        validate_environment()
    except Exception as exc:  # ConfigurationError, or ImportError on psycopg2
        return [("environment", False, f"{type(exc).__name__}: {exc}")]
    return [
        ("environment", True, f"SYSTEM_MODE={os.getenv('SYSTEM_MODE', 'LOCAL')} ok")
    ]


def _database_checks() -> list[tuple[str, bool, str]]:
    """Return ``(name, ok, detail)`` for the database/RLS checks."""
    from config.env_validator import validate_database_health

    if not os.getenv("DATABASE_URL"):
        return [("database", True, "skipped: DATABASE_URL is not set (local mode)")]
    try:
        validate_database_health()
    except Exception as exc:
        return [("database", False, f"{type(exc).__name__}: {exc}")]
    return [("database", True, "connectivity, migrations and RLS verified")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", action="store_true", help="also check the database")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)

    results = _checks()
    if args.db:
        results.extend(_database_checks())

    failures = [entry for entry in results if not entry[1]]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not failures,
                    "checks": [
                        {"name": name, "ok": ok, "detail": detail}
                        for name, ok, detail in results
                    ],
                },
                indent=2,
            )
        )
    else:
        for name, ok, detail in results:
            print(f"[{'OK ' if ok else 'FAIL'}] {name}: {detail}")
        print("preflight passed" if not failures else "preflight FAILED")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
