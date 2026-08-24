"""Read-only operational status collection for the RC-1 dashboard.

The dashboard deliberately does not place, amend, or cancel orders.  It reads a
status document produced by existing operational jobs and reports missing data
as ``unknown`` rather than assuming a healthy trading system.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_FILE_ENV = "QUANT_INDIA_STATUS_FILE"
DEFAULT_STATUS_FILE = Path("var/operational_status.json")
REQUIRED_FIELDS = (
    "broker_health",
    "reconciliation",
    "kill_switch",
    "latest_experiment",
    "open_orders",
    "system_health",
)


def status_file_path(environ: dict[str, str] | None = None) -> Path:
    """Return the configured operational status document path."""
    environment = os.environ if environ is None else environ
    return Path(environment.get(STATUS_FILE_ENV, str(DEFAULT_STATUS_FILE)))


def collect_status(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Load a status snapshot, returning explicit unknown values if unavailable.

    A malformed or absent file is an operational warning, never a reason to
    fabricate a green health state.
    """
    path = status_file_path(environ)
    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(path),
        "status_file": "unavailable",
        **{field: "unknown" for field in REQUIRED_FIELDS},
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("status document must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        snapshot["status_error"] = str(error)
        return snapshot

    snapshot.update({field: loaded.get(field, "unknown") for field in REQUIRED_FIELDS})
    snapshot["generated_at"] = loaded.get("generated_at", snapshot["generated_at"])
    snapshot["status_file"] = "loaded"
    return snapshot
