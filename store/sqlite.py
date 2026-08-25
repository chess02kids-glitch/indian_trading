"""SQLite backends for the repository interfaces.

A single file-backed store exposing one repository object per protocol.
This is the default local persistence for paper trading and the reference
behaviour Agent 2's Supabase adapters must match (same interface, same
semantics, same concurrency guarantees).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from models.domain import (
    OrderIntent,
    OrderResult,
    Position,
    ReconciliationResult,
    ResearchResult,
)

__all__ = [
    "SQLiteOrderRepository",
    "SQLitePositionRepository",
    "SQLiteReconciliationRepository",
    "SQLiteResearchRepository",
    "SQLiteRunRepository",
    "SQLiteStore",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    internal_order_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_idempotency
    ON orders (idempotency_key);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT NOT NULL,
    exchange TEXT NOT NULL,
    position_json TEXT NOT NULL,
    PRIMARY KEY (symbol, exchange)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    details_json TEXT
);
CREATE TABLE IF NOT EXISTS research (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliation (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    result_json TEXT NOT NULL
);
"""


class _Connection:
    """Shared connection with a re-entrant lock (one-process paper system)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock, self.conn:
            self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()


class SQLiteOrderRepository:
    def __init__(self, connection: _Connection) -> None:
        self._conn = connection

    def save_intent(self, intent: OrderIntent) -> OrderIntent:
        with self._conn.lock, self._conn.conn:
            self._conn.conn.execute(
                "INSERT OR IGNORE INTO orders "
                "(internal_order_id, idempotency_key, intent_json, result_json) "
                "VALUES (?, ?, ?, NULL)",
                (
                    intent.internal_order_id,
                    intent.idempotency_key,
                    intent.model_dump_json(),
                ),
            )
        return intent

    def get_intent(self, internal_order_id: str) -> OrderIntent | None:
        with self._conn.lock:
            row = self._conn.conn.execute(
                "SELECT intent_json FROM orders WHERE internal_order_id = ?",
                (internal_order_id,),
            ).fetchone()
        return OrderIntent.model_validate_json(row["intent_json"]) if row else None

    def save_result(self, result: OrderResult) -> OrderResult:
        with self._conn.lock, self._conn.conn:
            self._conn.conn.execute(
                "UPDATE orders SET result_json = ? WHERE internal_order_id = ?",
                (result.model_dump_json(), result.internal_order_id),
            )
        return result

    def get_result(self, internal_order_id: str) -> OrderResult | None:
        with self._conn.lock:
            row = self._conn.conn.execute(
                "SELECT result_json FROM orders WHERE internal_order_id = ? "
                "AND result_json IS NOT NULL",
                (internal_order_id,),
            ).fetchone()
        return OrderResult.model_validate_json(row["result_json"]) if row else None

    def find_by_idempotency_key(self, idempotency_key: str) -> OrderResult | None:
        with self._conn.lock:
            row = self._conn.conn.execute(
                "SELECT result_json FROM orders WHERE idempotency_key = ? "
                "AND result_json IS NOT NULL",
                (idempotency_key,),
            ).fetchone()
        return OrderResult.model_validate_json(row["result_json"]) if row else None

    def list_intents(self) -> list[OrderIntent]:
        with self._conn.lock:
            rows = self._conn.conn.execute(
                "SELECT intent_json FROM orders ORDER BY rowid"
            ).fetchall()
        return [OrderIntent.model_validate_json(r["intent_json"]) for r in rows]


class SQLitePositionRepository:
    def __init__(self, connection: _Connection) -> None:
        self._conn = connection

    def upsert_position(self, position: Position) -> Position:
        with self._conn.lock, self._conn.conn:
            self._conn.conn.execute(
                "INSERT OR REPLACE INTO positions (symbol, exchange, position_json) "
                "VALUES (?, ?, ?)",
                (position.symbol, position.exchange, position.model_dump_json()),
            )
        return position

    def get_position(self, symbol: str) -> Position | None:
        with self._conn.lock:
            rows = self._conn.conn.execute(
                "SELECT position_json FROM positions WHERE symbol = ?", (symbol,)
            ).fetchall()
        return Position.model_validate_json(rows[0]["position_json"]) if rows else None

    def list_positions(self) -> list[Position]:
        with self._conn.lock:
            rows = self._conn.conn.execute(
                "SELECT position_json FROM positions"
            ).fetchall()
        return [Position.model_validate_json(r["position_json"]) for r in rows]


class SQLiteRunRepository:
    def __init__(self, connection: _Connection) -> None:
        self._conn = connection

    def claim_run(self, run_id: str, *, resume_awaiting_approval: bool = False) -> bool:
        """Atomically claim a new run or explicitly resume approval-pending work."""
        if not run_id:
            return False
        with self._conn.lock, self._conn.conn:
            if resume_awaiting_approval:
                cursor = self._conn.conn.execute(
                    "UPDATE runs SET status = 'claimed' WHERE run_id = ? "
                    "AND status = 'awaiting_approval'",
                    (run_id,),
                )
                if cursor.rowcount == 1:
                    return True
            cursor = self._conn.conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, status, details_json) "
                "VALUES (?, 'claimed', NULL)",
                (run_id,),
            )
            return cursor.rowcount == 1

    def save_run(
        self, run_id: str, status: str, details: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        with self._conn.lock, self._conn.conn:
            self._conn.conn.execute(
                "INSERT INTO runs (run_id, status, details_json) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, "
                "details_json = excluded.details_json",
                (run_id, status, json.dumps(dict(details or {}), default=str)),
            )
        record = self.get_run(run_id)
        if record is None:  # pragma: no cover - defensive: insert just succeeded
            raise RuntimeError(f"run {run_id!r} missing immediately after save")
        return record

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn.lock:
            row = self._conn.conn.execute(
                "SELECT run_id, status, details_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "details": json.loads(row["details_json"] or "{}"),
        }

    def list_runs(self) -> list[dict[str, Any]]:
        with self._conn.lock:
            rows = self._conn.conn.execute(
                "SELECT run_id, status, details_json FROM runs ORDER BY rowid"
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "status": r["status"],
                "details": json.loads(r["details_json"] or "{}"),
            }
            for r in rows
        ]


class SQLiteResearchRepository:
    def __init__(self, connection: _Connection) -> None:
        self._conn = connection

    def save_result(self, result: ResearchResult) -> ResearchResult:
        with self._conn.lock, self._conn.conn:
            self._conn.conn.execute(
                "INSERT INTO research (result_json) VALUES (?)",
                (result.model_dump_json(),),
            )
        return result

    def latest_result(self) -> ResearchResult | None:
        with self._conn.lock:
            row = self._conn.conn.execute(
                "SELECT result_json FROM research ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return ResearchResult.model_validate_json(row["result_json"]) if row else None

    def list_by_hypothesis(self, hypothesis_id: str) -> list[ResearchResult]:
        with self._conn.lock:
            rows = self._conn.conn.execute(
                "SELECT result_json FROM research ORDER BY seq"
            ).fetchall()
        results = [
            ResearchResult.model_validate_json(row["result_json"]) for row in rows
        ]
        return [r for r in results if r.hypothesis_id == hypothesis_id]


class SQLiteReconciliationRepository:
    def __init__(self, connection: _Connection) -> None:
        self._conn = connection

    def save_result(self, result: ReconciliationResult) -> ReconciliationResult:
        with self._conn.lock, self._conn.conn:
            self._conn.conn.execute(
                "INSERT INTO reconciliation (result_json) VALUES (?)",
                (result.model_dump_json(),),
            )
        return result

    def latest_result(self, run_id: str | None = None) -> ReconciliationResult | None:
        with self._conn.lock:
            if run_id is None:
                row = self._conn.conn.execute(
                    "SELECT result_json FROM reconciliation ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                return (
                    ReconciliationResult.model_validate_json(row["result_json"])
                    if row
                    else None
                )
            rows = self._conn.conn.execute(
                "SELECT result_json FROM reconciliation ORDER BY seq DESC"
            ).fetchall()
        for row in rows:
            result = ReconciliationResult.model_validate_json(row["result_json"])
            if result.run_id == run_id:
                return result
        return None

    def list_results(self) -> list[ReconciliationResult]:
        with self._conn.lock:
            rows = self._conn.conn.execute(
                "SELECT result_json FROM reconciliation ORDER BY seq"
            ).fetchall()
        return [
            ReconciliationResult.model_validate_json(r["result_json"]) for r in rows
        ]


class SQLiteStore:
    """Factory for the SQLite repository objects sharing one database file."""

    def __init__(self, path: str | Path = "var/paper.sqlite3") -> None:
        self.path = Path(path)
        self._connection = _Connection(self.path)
        self.orders = SQLiteOrderRepository(self._connection)
        self.positions = SQLitePositionRepository(self._connection)
        self.runs = SQLiteRunRepository(self._connection)
        self.research = SQLiteResearchRepository(self._connection)
        self.reconciliation = SQLiteReconciliationRepository(self._connection)

    def close(self) -> None:
        self._connection.close()
