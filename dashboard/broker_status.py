"""Read-only broker-health data access for the broker dashboard.

Mirrors :mod:`dashboard.operational`: the dashboard never assumes a healthy
system — a missing or malformed broker status document renders every field
as ``unknown``. The dashboard never talks to a broker, never executes, and
exposes no buttons that could place, amend, or cancel orders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from broker.status import (
    BROKER_STATUS_FIELDS,
    BROKER_STATUS_FILE_ENV,
    DEFAULT_BROKER_STATUS_FILE,
    broker_status_file_path,
)

__all__ = [
    "BROKER_STATUS_FIELDS",
    "BROKER_STATUS_FILE_ENV",
    "DEFAULT_BROKER_STATUS_FILE",
    "broker_status_file_path",
    "collect_broker_dashboard_status",
    "summarize_broker_health",
]

#: Cap on the number of recent orders rendered (read-only slice).
RECENT_ORDER_LIMIT = 15


def collect_broker_dashboard_status(
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Load the broker status snapshot, unknown when unavailable.

    A malformed or absent document is an operational warning, never a reason
    to fabricate a green broker-health state.
    """
    path = broker_status_file_path(environ)
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(path),
        "status_file": "unavailable",
        **{field: "unknown" for field in BROKER_STATUS_FIELDS},
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("broker status document must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        snapshot["status_error"] = str(error)
        return snapshot

    snapshot.update(
        {field: loaded.get(field, "unknown") for field in BROKER_STATUS_FIELDS}
    )
    snapshot["generated_at"] = loaded.get("generated_at", snapshot["generated_at"])
    snapshot["status_file"] = "loaded"
    return snapshot


def summarize_broker_health(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce the broker status snapshot to compact dashboard fields."""
    if not snapshot or snapshot.get("status_file") != "loaded":
        return {
            "overall": "unknown",
            "connectivity": {},
            "tokens": {},
            "sandbox_health": "unknown",
            "reconciliation": "unknown",
            "recent_orders": [],
        }

    connectivity = snapshot.get("broker_connectivity")
    if not isinstance(connectivity, dict):
        connectivity = {}
    tokens = snapshot.get("token_status")
    if not isinstance(tokens, dict):
        tokens = {}
    orders = snapshot.get("recent_sandbox_orders")
    if not isinstance(orders, list):
        orders = []

    overall = str(snapshot.get("sandbox_health", "unknown"))
    return {
        "overall": overall,
        "connectivity": connectivity,
        "tokens": tokens,
        "sandbox_health": overall,
        "reconciliation": snapshot.get("reconciliation_health", "unknown"),
        "recent_orders": orders[-RECENT_ORDER_LIMIT:],
        "generated_at": snapshot.get("generated_at"),
        "source": snapshot.get("source"),
    }
