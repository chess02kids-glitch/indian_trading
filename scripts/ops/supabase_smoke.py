#!/usr/bin/env python3
"""Safe production-schema verification for Quant India; emits one JSON report."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

REQUIRED_TABLES = ("orders", "positions", "executions", "reconciliation_log")
REQUIRED_COLUMNS = {
    "orders": {
        "id",
        "user_id",
        "internal_order_id",
        "idempotency_key",
        "symbol",
        "side",
        "quantity",
        "price",
        "status",
    },
    "positions": {"user_id", "symbol", "quantity", "average_price"},
    "executions": {
        "order_id",
        "broker_execution_id",
        "executed_quantity",
        "executed_price",
    },
    "reconciliation_log": {"run_id", "status", "matched", "discrepancy_details"},
}


def main() -> int:
    report: dict[str, Any] = {"ok": False, "checks": {}, "schema_drift": []}
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        report["error"] = "DATABASE_URL is not configured"
        print(json.dumps(report, sort_keys=True))
        return 2
    try:
        import psycopg2

        expected = sorted(
            p.name for p in Path("migrations").glob("[0-9][0-9][0-9]_*.sql")
        )
        with psycopg2.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user, version()")
                db, user, version = cur.fetchone()
                # User/database names and version are metadata, not credentials.
                report["checks"]["identity"] = {
                    "database": db,
                    "role": user,
                    "server": version.split(",")[0],
                }
                cur.execute(
                    "SELECT version FROM public.schema_migrations ORDER BY version"
                )
                applied = [r[0] for r in cur.fetchall()]
                report["checks"]["migrations"] = {
                    "expected": expected,
                    "applied": applied,
                    "complete": set(expected) <= set(applied),
                }
                cur.execute(
                    "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'"
                )
                cols: dict[str, set[str]] = {}
                for table, col in cur.fetchall():
                    cols.setdefault(table, set()).add(col)
                report["checks"]["tables"] = {
                    t: sorted(cols.get(t, set())) for t in REQUIRED_TABLES
                }
                for table, required in REQUIRED_COLUMNS.items():
                    missing = sorted(required - cols.get(table, set()))
                    if missing:
                        report["schema_drift"].append(
                            {"table": table, "missing_columns": missing}
                        )
                cur.execute(
                    "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname='public' AND tablename = ANY(%s)",
                    (list(REQUIRED_TABLES),),
                )
                rls = dict(cur.fetchall())
                report["checks"]["rls_enabled"] = rls
                cur.execute(
                    "SELECT tablename, policyname, cmd FROM pg_policies WHERE schemaname='public' AND tablename = ANY(%s) ORDER BY tablename, policyname",
                    (list(REQUIRED_TABLES),),
                )
                report["checks"]["rls_policies"] = [
                    {"table": a, "name": b, "command": c} for a, b, c in cur.fetchall()
                ]
                cur.execute(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' AND tablename = ANY(%s)",
                    (list(REQUIRED_TABLES),),
                )
                indexes = cur.fetchall()
                report["checks"]["indexes"] = [a for a, _ in indexes]
                report["checks"]["idempotency_unique_index"] = any(
                    "idempotency_key" in d and "UNIQUE" in d.upper() for _, d in indexes
                )
                cur.execute("SAVEPOINT quant_india_v05_smoke")
                cur.execute(
                    "CREATE TEMP TABLE quant_india_v05_smoke (id integer PRIMARY KEY, value text) ON COMMIT DROP"
                )
                cur.execute("INSERT INTO quant_india_v05_smoke VALUES (1, 'ok')")
                cur.execute("SELECT value FROM quant_india_v05_smoke WHERE id=1")
                report["checks"]["rollback_crud"] = cur.fetchone()[0] == "ok"
                cur.execute("ROLLBACK TO SAVEPOINT quant_india_v05_smoke")
                # v0.5 local runner uses SQLite claims; report whether production has an equivalent.
                report["checks"]["run_claim_storage"] = {
                    "table": "reconciliation_log",
                    "run_id_present": "run_id" in cols.get("reconciliation_log", set()),
                    "unique_run_id_index": any(
                        "run_id" in d and "UNIQUE" in d.upper() for _, d in indexes
                    ),
                }
                conn.rollback()
        checks = report["checks"]
        report["ok"] = bool(
            checks["migrations"]["complete"]
            and not report["schema_drift"]
            and checks["rollback_crud"]
            and checks["idempotency_unique_index"]
            and checks["run_claim_storage"]["unique_run_id_index"]
        )
    except Exception as exc:
        report["error"] = type(exc).__name__
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
