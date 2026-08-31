"""Durable local ledger for the virtual paper portfolio.

All money values are virtual INR values.  The SQLite database is intentionally
kept under ``var/`` (which is ignored by Git), separate from broker accounts
and credentials.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

DEFAULT_CAPITAL = 1_000_000.0


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PaperLedger:
    """Own all local persistent state for one paper account."""

    def __init__(self, path: Path | str = "var/paper_trading.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialise(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS paper_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    initial_capital REAL NOT NULL,
                    cash REAL NOT NULL,
                    running INTEGER NOT NULL DEFAULT 0,
                    data_mode TEXT NOT NULL DEFAULT 'UPSTOX_DATA',
                    active_strategy TEXT,
                    started_at TEXT,
                    paused_at TEXT,
                    watchlist_json TEXT NOT NULL DEFAULT '["NIFTY_50","RELIANCE","HDFCBANK","ICICIBANK","TCS"]',
                    risk_policy_json TEXT NOT NULL DEFAULT '{}',
                    auto_paper_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_strategy TEXT,
                    auto_interval_seconds INTEGER NOT NULL DEFAULT 86400,
                    last_auto_rebalance_at TEXT,
                    benchmark_symbol TEXT NOT NULL DEFAULT 'NIFTY_50',
                    benchmark_start_price REAL,
                    last_quote_success_at TEXT,
                    last_quote_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY,
                    quantity INTEGER NOT NULL CHECK(quantity >= 0),
                    average_entry_cost REAL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    opened_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                    quantity INTEGER NOT NULL CHECK(quantity > 0),
                    reference_price REAL NOT NULL,
                    fill_price REAL,
                    notional REAL NOT NULL,
                    charges REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    reason TEXT,
                    source TEXT NOT NULL,
                    quote_timestamp TEXT
                );
                CREATE TABLE IF NOT EXISTS paper_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    instrument_key TEXT NOT NULL,
                    last_price REAL NOT NULL,
                    bid_price REAL,
                    ask_price REAL,
                    volume REAL,
                    source_timestamp TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_paper_marks_symbol_time
                    ON paper_marks(symbol, recorded_at DESC);
                CREATE TABLE IF NOT EXISTS paper_equity_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    quote_status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                """
            )
            # Existing local paper ledgers are upgraded in place.  These are
            # additive fields only, so opening a dashboard never discards a
            # virtual account or its audit history.
            self._add_column_if_missing(
                conn,
                "paper_settings",
                "watchlist_json",
                'TEXT NOT NULL DEFAULT \'["NIFTY_50","RELIANCE","HDFCBANK","ICICIBANK","TCS"]\'',
            )
            self._add_column_if_missing(
                conn, "paper_settings", "risk_policy_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            self._add_column_if_missing(
                conn,
                "paper_settings",
                "auto_paper_enabled",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._add_column_if_missing(conn, "paper_settings", "auto_strategy", "TEXT")
            self._add_column_if_missing(
                conn,
                "paper_settings",
                "auto_interval_seconds",
                "INTEGER NOT NULL DEFAULT 86400",
            )
            self._add_column_if_missing(
                conn, "paper_settings", "last_auto_rebalance_at", "TEXT"
            )
            self._add_column_if_missing(
                conn,
                "paper_settings",
                "benchmark_symbol",
                "TEXT NOT NULL DEFAULT 'NIFTY_50'",
            )
            self._add_column_if_missing(
                conn, "paper_settings", "benchmark_start_price", "REAL"
            )
            self._add_column_if_missing(
                conn, "paper_settings", "last_quote_success_at", "TEXT"
            )
            self._add_column_if_missing(
                conn, "paper_settings", "last_quote_error", "TEXT"
            )
            row = conn.execute("SELECT id FROM paper_settings WHERE id = 1").fetchone()
            if row is None:
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO paper_settings
                    (id, initial_capital, cash, running, data_mode, updated_at)
                    VALUES (1, ?, ?, 0, 'UPSTOX_DATA', ?)
                    """,
                    (DEFAULT_CAPITAL, DEFAULT_CAPITAL, now),
                )

    @staticmethod
    def _add_column_if_missing(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def settings(self) -> dict[str, Any]:
        with self._connection() as conn:
            result = self._row(
                conn.execute("SELECT * FROM paper_settings WHERE id = 1").fetchone()
            )
        assert result is not None
        result["running"] = bool(result["running"])
        result["auto_paper_enabled"] = bool(result["auto_paper_enabled"])
        try:
            result["watchlist"] = list(json.loads(result.pop("watchlist_json")))
        except (TypeError, ValueError):
            result.pop("watchlist_json", None)
            result["watchlist"] = []
        try:
            result["risk_policy"] = dict(json.loads(result.pop("risk_policy_json")))
        except (TypeError, ValueError):
            result.pop("risk_policy_json", None)
            result["risk_policy"] = {}
        return result

    def configure(self, capital: float, data_mode: str) -> dict[str, Any]:
        if capital <= 0:
            raise ValueError("virtual capital must be positive")
        mode = str(data_mode).strip().upper()
        if mode not in {"UPSTOX_DATA", "SANDBOX"}:
            raise ValueError("data mode must be UPSTOX_DATA or SANDBOX")
        with self._connection() as conn:
            positions = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_positions WHERE quantity > 0"
            ).fetchone()
            orders = conn.execute("SELECT COUNT(*) AS n FROM paper_orders").fetchone()
            if (positions and int(positions["n"]) > 0) or (
                orders and int(orders["n"]) > 0
            ):
                raise ValueError(
                    "reset the paper account before changing virtual capital"
                )
            now = utc_now()
            conn.execute(
                """
                UPDATE paper_settings
                SET initial_capital = ?, cash = ?, data_mode = ?, updated_at = ?
                WHERE id = 1
                """,
                (float(capital), float(capital), mode, now),
            )
            self._event(
                conn, "portfolio_configured", {"capital": capital, "data_mode": mode}
            )
        return self.settings()

    def start(self, strategy_id: str | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            now = utc_now()
            conn.execute(
                """
                UPDATE paper_settings
                SET running = 1, active_strategy = ?, started_at = ?, paused_at = NULL, updated_at = ?
                WHERE id = 1
                """,
                (strategy_id, now, now),
            )
            self._event(conn, "paper_started", {"strategy_id": strategy_id})
        return self.settings()

    def pause(self) -> dict[str, Any]:
        with self._connection() as conn:
            now = utc_now()
            conn.execute(
                "UPDATE paper_settings SET running = 0, paused_at = ?, updated_at = ? WHERE id = 1",
                (now, now),
            )
            self._event(conn, "paper_paused", {})
        return self.settings()

    def set_watchlist(self, symbols: Sequence[str]) -> dict[str, Any]:
        cleaned = list(
            dict.fromkeys(
                str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
            )
        )
        if not cleaned:
            raise ValueError("watchlist must contain at least one symbol")
        if len(cleaned) > 50:
            raise ValueError("watchlist is limited to 50 instruments")
        with self._connection() as conn:
            conn.execute(
                "UPDATE paper_settings SET watchlist_json = ?, updated_at = ? WHERE id = 1",
                (json.dumps(cleaned), utc_now()),
            )
            self._event(conn, "watchlist_updated", {"symbols": cleaned})
        return self.settings()

    def set_risk_policy(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        with self._connection() as conn:
            conn.execute(
                "UPDATE paper_settings SET risk_policy_json = ?, updated_at = ? WHERE id = 1",
                (json.dumps(dict(policy), sort_keys=True), utc_now()),
            )
            self._event(conn, "risk_policy_updated", dict(policy))
        return self.settings()

    def set_auto_paper(
        self, *, enabled: bool, strategy_id: str | None, interval_seconds: int
    ) -> dict[str, Any]:
        if interval_seconds < 60:
            raise ValueError("automatic paper interval must be at least 60 seconds")
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE paper_settings
                SET auto_paper_enabled = ?, auto_strategy = ?, auto_interval_seconds = ?,
                    last_auto_rebalance_at = NULL, updated_at = ?
                WHERE id = 1
                """,
                (
                    int(enabled),
                    strategy_id if enabled else None,
                    interval_seconds,
                    utc_now(),
                ),
            )
            self._event(
                conn,
                "auto_paper_changed",
                {
                    "enabled": bool(enabled),
                    "strategy_id": strategy_id if enabled else None,
                },
            )
        return self.settings()

    def mark_auto_rebalance(self) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE paper_settings SET last_auto_rebalance_at = ?, updated_at = ? WHERE id = 1",
                (utc_now(), utc_now()),
            )

    def record_quote_health(self, error: str | None) -> None:
        with self._connection() as conn:
            now = utc_now()
            if error:
                conn.execute(
                    "UPDATE paper_settings SET last_quote_error = ?, updated_at = ? WHERE id = 1",
                    (error, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE paper_settings
                    SET last_quote_success_at = ?, last_quote_error = NULL, updated_at = ?
                    WHERE id = 1
                    """,
                    (now, now),
                )

    def set_benchmark_start_price(self, price: float) -> None:
        if price <= 0:
            return
        with self._connection() as conn:
            existing = conn.execute(
                "SELECT benchmark_start_price FROM paper_settings WHERE id = 1"
            ).fetchone()
            if existing and existing["benchmark_start_price"] is None:
                conn.execute(
                    "UPDATE paper_settings SET benchmark_start_price = ?, updated_at = ? WHERE id = 1",
                    (price, utc_now()),
                )
                self._event(
                    conn, "benchmark_started", {"symbol": "NIFTY_50", "price": price}
                )

    def reset(self, capital: float | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            current = conn.execute(
                "SELECT initial_capital FROM paper_settings WHERE id = 1"
            ).fetchone()
            selected = float(
                capital if capital is not None else current["initial_capital"]
            )
            if selected <= 0:
                raise ValueError("virtual capital must be positive")
            now = utc_now()
            conn.execute("DELETE FROM paper_positions")
            conn.execute("DELETE FROM paper_orders")
            conn.execute("DELETE FROM paper_marks")
            conn.execute("DELETE FROM paper_equity_snapshots")
            conn.execute(
                """
                UPDATE paper_settings
                SET initial_capital = ?, cash = ?, running = 0, active_strategy = NULL,
                    auto_paper_enabled = 0, auto_strategy = NULL,
                    last_auto_rebalance_at = NULL, benchmark_start_price = NULL,
                    last_quote_success_at = NULL, last_quote_error = NULL,
                    started_at = NULL, paused_at = ?, updated_at = ?
                WHERE id = 1
                """,
                (selected, selected, now, now),
            )
            self._event(conn, "paper_reset", {"capital": selected})
        return self.settings()

    def positions(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions WHERE quantity > 0 ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def all_positions(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_positions ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def realized_pnl_total(self) -> float:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS value FROM paper_positions"
            ).fetchone()
        return float(row["value"])

    def order_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_orders ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_orders(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM paper_orders ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def marks_history(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_marks ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 10_000)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def record_event(self, event_type: str, detail: Mapping[str, Any]) -> None:
        with self._connection() as conn:
            self._event(conn, event_type, detail)

    def record_order(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        quantity: int,
        reference_price: float,
        fill_price: float | None,
        notional: float,
        charges: float,
        status: str,
        reason: str | None,
        source: str,
        quote_timestamp: str | None,
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO paper_orders
                (created_at, strategy_id, symbol, side, quantity, reference_price, fill_price,
                 notional, charges, status, reason, source, quote_timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    strategy_id,
                    symbol,
                    side,
                    quantity,
                    reference_price,
                    fill_price,
                    notional,
                    charges,
                    status,
                    reason,
                    source,
                    quote_timestamp,
                ),
            )

    def execute_virtual_fill(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side: str,
        quantity: int,
        fill_price: float,
        charges: float,
        source: str,
        quote_timestamp: str | None,
    ) -> dict[str, Any]:
        """Atomically apply one fully-filled virtual cash-equity order."""
        if (
            side not in {"BUY", "SELL"}
            or quantity <= 0
            or fill_price <= 0
            or charges < 0
        ):
            raise ValueError("invalid virtual fill")
        notional = float(quantity) * float(fill_price)
        with self._connection() as conn:
            settings = conn.execute(
                "SELECT cash FROM paper_settings WHERE id = 1"
            ).fetchone()
            position = conn.execute(
                "SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)
            ).fetchone()
            held = int(position["quantity"]) if position else 0
            cash = float(settings["cash"])
            if side == "BUY":
                total = notional + charges
                if total > cash + 1e-8:
                    self._insert_order(
                        conn,
                        strategy_id,
                        symbol,
                        side,
                        quantity,
                        fill_price,
                        None,
                        notional,
                        charges,
                        "REJECTED",
                        "insufficient virtual cash",
                        source,
                        quote_timestamp,
                    )
                    return {
                        "status": "REJECTED",
                        "reason": "insufficient virtual cash",
                        "symbol": symbol,
                    }
                existing_cost = (
                    float(position["average_entry_cost"] or 0.0) * held
                    if position
                    else 0.0
                )
                new_qty = held + quantity
                avg_cost = (existing_cost + total) / new_qty
                realized = float(position["realized_pnl"] or 0.0) if position else 0.0
                conn.execute(
                    """
                    INSERT INTO paper_positions(symbol, quantity, average_entry_cost, realized_pnl, opened_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET quantity=excluded.quantity,
                        average_entry_cost=excluded.average_entry_cost,
                        realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at
                    """,
                    (symbol, new_qty, avg_cost, realized, utc_now(), utc_now()),
                )
                conn.execute(
                    "UPDATE paper_settings SET cash = cash - ?, updated_at = ? WHERE id = 1",
                    (total, utc_now()),
                )
            else:
                if quantity > held:
                    self._insert_order(
                        conn,
                        strategy_id,
                        symbol,
                        side,
                        quantity,
                        fill_price,
                        None,
                        notional,
                        charges,
                        "REJECTED",
                        "insufficient virtual holdings",
                        source,
                        quote_timestamp,
                    )
                    return {
                        "status": "REJECTED",
                        "reason": "insufficient virtual holdings",
                        "symbol": symbol,
                    }
                avg_cost = float(position["average_entry_cost"] or 0.0)
                realized = float(position["realized_pnl"] or 0.0) + (
                    notional - charges - avg_cost * quantity
                )
                remaining = held - quantity
                if remaining:
                    conn.execute(
                        "UPDATE paper_positions SET quantity=?, realized_pnl=?, updated_at=? WHERE symbol=?",
                        (remaining, realized, utc_now(), symbol),
                    )
                else:
                    # Retain the zero-quantity row so lifetime realised P&L
                    # remains auditable after a position is closed.
                    conn.execute(
                        "UPDATE paper_positions SET quantity=0, realized_pnl=?, updated_at=? WHERE symbol=?",
                        (realized, utc_now(), symbol),
                    )
                conn.execute(
                    "UPDATE paper_settings SET cash = cash + ?, updated_at = ? WHERE id = 1",
                    (notional - charges, utc_now()),
                )
            self._insert_order(
                conn,
                strategy_id,
                symbol,
                side,
                quantity,
                fill_price,
                fill_price,
                notional,
                charges,
                "FILLED",
                None,
                source,
                quote_timestamp,
            )
            self._event(
                conn,
                "virtual_order_filled",
                {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                },
            )
        return {
            "status": "FILLED",
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "charges": charges,
        }

    def _insert_order(
        self,
        conn: sqlite3.Connection,
        strategy_id: str,
        symbol: str,
        side: str,
        quantity: int,
        reference_price: float,
        fill_price: float | None,
        notional: float,
        charges: float,
        status: str,
        reason: str | None,
        source: str,
        quote_timestamp: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO paper_orders
            (created_at, strategy_id, symbol, side, quantity, reference_price, fill_price,
             notional, charges, status, reason, source, quote_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                strategy_id,
                symbol,
                side,
                quantity,
                reference_price,
                fill_price,
                notional,
                charges,
                status,
                reason,
                source,
                quote_timestamp,
            ),
        )

    def record_marks(self, quotes: Sequence[Mapping[str, Any]]) -> None:
        if not quotes:
            return
        now = utc_now()
        rows = [
            (
                now,
                str(q["symbol"]),
                str(q["instrument_key"]),
                float(q["last_price"]),
                q.get("bid_price"),
                q.get("ask_price"),
                q.get("volume"),
                str(q["timestamp"]),
                str(q.get("source", "upstox")),
            )
            for q in quotes
        ]
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO paper_marks
                (recorded_at, symbol, instrument_key, last_price, bid_price, ask_price, volume, source_timestamp, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def latest_marks(
        self, symbols: Sequence[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        with self._connection() as conn:
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"""
                    SELECT m.* FROM paper_marks m
                    INNER JOIN (
                      SELECT symbol, MAX(id) AS max_id FROM paper_marks
                      WHERE symbol IN ({placeholders}) GROUP BY symbol
                    ) latest ON m.id = latest.max_id
                    """,
                    tuple(symbols),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT m.* FROM paper_marks m
                    INNER JOIN (SELECT symbol, MAX(id) AS max_id FROM paper_marks GROUP BY symbol) latest
                    ON m.id = latest.max_id
                    """
                ).fetchall()
        return {str(row["symbol"]): dict(row) for row in rows}

    def record_equity(self, snapshot: Mapping[str, Any]) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO paper_equity_snapshots
                (recorded_at, equity, cash, market_value, realized_pnl, unrealized_pnl, quote_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    float(snapshot["equity"]),
                    float(snapshot["cash"]),
                    float(snapshot["market_value"]),
                    float(snapshot["realized_pnl"]),
                    float(snapshot["unrealized_pnl"]),
                    str(snapshot["quote_status"]),
                ),
            )

    def equity_history(self, limit: int = 240) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_equity_snapshots ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 5000)),),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def latest_equity(self) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM paper_equity_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._row(row)

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_events ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    @staticmethod
    def _event(
        conn: sqlite3.Connection, event_type: str, detail: Mapping[str, Any]
    ) -> None:
        conn.execute(
            "INSERT INTO paper_events(created_at, event_type, detail_json) VALUES (?, ?, ?)",
            (utc_now(), event_type, json.dumps(dict(detail), sort_keys=True)),
        )
