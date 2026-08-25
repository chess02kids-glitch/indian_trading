"""Read-only broker health status collection.

Produces the machine-readable document consumed by the CLI (``broker
health``) and the Streamlit broker dashboard. The document is persisted to
``var/broker_status.json`` (override with ``QUANT_BROKER_STATUS_FILE``) so
the dashboard never talks to a broker itself — dashboards read, they never
execute.

Fields: broker connectivity, token status, sandbox health, reconciliation
health, and recent sandbox orders. Missing data is reported as ``unknown`` —
never silently assumed healthy.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from broker.interface import BrokerAdapter
from broker.token import TokenManager

__all__ = [
    "BROKER_STATUS_FILE_ENV",
    "DEFAULT_BROKER_STATUS_FILE",
    "BROKER_STATUS_FIELDS",
    "broker_status_file_path",
    "collect_broker_health",
    "write_broker_status",
    "merge_recent_orders",
    "order_entry",
    "MAX_RECENT_ORDERS",
]

BROKER_STATUS_FILE_ENV = "QUANT_BROKER_STATUS_FILE"
DEFAULT_BROKER_STATUS_FILE = Path("var/broker_status.json")

BROKER_STATUS_FIELDS = (
    "broker_connectivity",
    "token_status",
    "sandbox_health",
    "reconciliation_health",
    "recent_sandbox_orders",
)

MAX_RECENT_ORDERS = 25


def broker_status_file_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the configured broker status document path."""
    source = os.environ if environ is None else environ
    return Path(source.get(BROKER_STATUS_FILE_ENV, str(DEFAULT_BROKER_STATUS_FILE)))


def order_entry(result: Any) -> dict[str, Any]:
    """Serialise one OrderResult for the recent-orders list."""
    return {
        "internal_order_id": result.internal_order_id,
        "broker_order_id": result.broker_order_id,
        "symbol": result.symbol,
        "side": result.side.value if result.side else None,
        "status": result.status.value,
        "requested_quantity": result.requested_quantity,
        "filled_quantity": result.filled_quantity,
        "average_fill_price": result.average_fill_price,
        "reason": result.reason,
        "timestamp": (result.timestamp.isoformat() if result.timestamp else None),
    }


def merge_recent_orders(
    existing: Sequence[Mapping[str, Any]] | None,
    new_entries: Sequence[Mapping[str, Any]],
    *,
    limit: int = MAX_RECENT_ORDERS,
) -> list[dict[str, Any]]:
    """Append new order entries, deduplicating by internal order id."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in list(existing or []) + [dict(e) for e in new_entries]:
        entry_id = str(entry.get("internal_order_id", ""))
        if not entry_id:
            continue
        if entry_id not in merged:
            order.append(entry_id)
        merged[entry_id] = dict(entry)
    keep = order[-limit:]
    return [merged[key] for key in keep]


def _connectivity(adapter: BrokerAdapter) -> str:
    try:
        return "connected" if adapter.ping() else "unreachable"
    except Exception as exc:  # any transport surprise => disconnected
        return f"error: {exc.__class__.__name__}"


def collect_broker_health(
    adapters: Mapping[str, BrokerAdapter] | None = None,
    token_manager: TokenManager | None = None,
    *,
    reconciliation_health: Any = "unknown",
    recent_sandbox_orders: Sequence[Mapping[str, Any]] | None = None,
    clock: Any = None,
) -> dict[str, Any]:
    """Build the broker health status document (read-only).

    ``adapters`` maps broker name -> adapter. Reconciliation health is
    injected (usually the engine's latest persisted result) so collection
    itself never triggers reconciliation work.
    """
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    brokers = dict(adapters or {})
    connectivity: dict[str, str] = {}
    tokens: dict[str, Any] = {}
    healthy = True
    for name in sorted(brokers):
        adapter = brokers[name]
        connectivity[name] = _connectivity(adapter)
        manager = token_manager or adapter.token_manager
        token_status = manager.status(name)
        tokens[name] = token_status.to_dict()
        if connectivity[name] != "connected":
            healthy = False
        if token_status.state in ("expired", "missing"):
            healthy = False

    if not brokers:
        sandbox_health = "unknown"
    elif healthy:
        sandbox_health = "healthy"
    else:
        sandbox_health = "degraded"

    return {
        "generated_at": moment.isoformat(),
        "broker_connectivity": connectivity or "unknown",
        "token_status": tokens or "unknown",
        "sandbox_health": sandbox_health,
        "reconciliation_health": reconciliation_health,
        "recent_sandbox_orders": [dict(e) for e in (recent_sandbox_orders or [])],
    }


def write_broker_status(
    document: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Atomically persist the status document; returns the path."""
    path = broker_status_file_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(document), handle, indent=2, sort_keys=True, default=str)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def load_prior_status(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Best-effort load of the previous status document (for merge)."""
    path = broker_status_file_path(environ)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def summarize_reconciliation(result: Any) -> dict[str, Any]:
    """Compact reconciliation outcome for the status document."""
    if result is None:
        return {"state": "unknown"}
    return {
        "state": "matched" if result.matched else "locked",
        "matched": bool(result.matched),
        "locked": bool(result.locked),
        "mismatches": len(result.mismatches),
        "as_of": result.as_of.isoformat() if result.as_of else None,
    }
