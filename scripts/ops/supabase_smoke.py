#!/usr/bin/env python3
"""Read-only/rollback Supabase smoke check. Emits one secret-free JSON object."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    result: dict[str, object] = {"ok": False, "checks": {}}
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        result["error"] = "DATABASE_URL is not configured"
        print(json.dumps(result, sort_keys=True))
        return 2
    try:
        import psycopg2

        expected_migrations = sorted(p.name for p in Path("migrations").glob("[0-9][0-9][0-9]_*.sql"))
        with psycopg2.connect(database_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM public.schema_migrations ORDER BY version")
                applied = [row[0] for row in cur.fetchall()]
                result["checks"] = {"connectivity": True,
                    "migrations": {"expected": expected_migrations, "applied": applied,
                                   "complete": set(expected_migrations).issubset(applied)}}
                cur.execute("SELECT to_regclass('public.orders'), to_regclass('public.positions'), to_regclass('public.executions'), to_regclass('public.reconciliation_log')")
                tables = [row for row in cur.fetchone()]
                result["checks"]["tables"] = {"present": all(tables), "names": tables}
                cur.execute("SAVEPOINT quant_india_smoke")
                cur.execute("CREATE TEMP TABLE quant_india_smoke (id integer PRIMARY KEY, value text) ON COMMIT DROP")
                cur.execute("INSERT INTO quant_india_smoke VALUES (1, 'ok')")
                cur.execute("SELECT value FROM quant_india_smoke WHERE id = 1")
                result["checks"]["rollback_crud"] = cur.fetchone()[0] == "ok"
                cur.execute("ROLLBACK TO SAVEPOINT quant_india_smoke")
                cur.execute("SELECT relrowsecurity FROM pg_class WHERE oid = 'public.orders'::regclass")
                result["checks"]["rls_orders_enabled"] = bool(cur.fetchone()[0])
                result["checks"]["atomic_run_claim"] = "requires RunRepository schema/function verification"
                conn.rollback()
        checks = result["checks"]
        result["ok"] = bool(checks["migrations"]["complete"] and checks["tables"]["present"] and checks["rollback_crud"])
    except Exception as exc:
        result["error"] = type(exc).__name__
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
