"""System health state: HEALTHY / WARNING / HALTED / LOCKED.

The health service is the single source of truth for the current system
state and persists a JSON status document that the dashboard and operators
read. State transitions are monotonic in severity within a run: once
HALTED or LOCKED, only an explicit operator reset (or a new run) returns
to a lower state.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

__all__ = ["HealthService", "SystemHealth"]


class SystemHealth(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    HALTED = "HALTED"
    LOCKED = "LOCKED"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY = {
    SystemHealth.HEALTHY: 0,
    SystemHealth.WARNING: 1,
    SystemHealth.HALTED: 2,
    SystemHealth.LOCKED: 3,
}


class HealthService:
    """Tracks and persists system health with a structured status document."""

    def __init__(self, status_path: str | Path = "var/operational_status.json") -> None:
        self.status_path = Path(status_path)
        # RLock: state writes call to_dict()/write while holding the lock.
        self._lock = threading.RLock()
        self._state = SystemHealth.HEALTHY
        self._reason: str | None = None
        self._updated_at: datetime = datetime.now(UTC)
        self._history: list[dict[str, Any]] = []

    @property
    def state(self) -> SystemHealth:
        with self._lock:
            return self._state

    def set_state(
        self, health: SystemHealth, reason: str | None = None, **context: Any
    ) -> SystemHealth:
        """Transition state. Lower-severity states never overwrite higher ones
        (fail closed): resetting requires :meth:`reset` by an operator."""
        with self._lock:
            if health.severity < self._state.severity:
                return self._state
            self._state = health
            self._reason = reason
            self._updated_at = datetime.now(UTC)
            self._history.append(
                {
                    "state": health.value,
                    "reason": reason,
                    "at": self._updated_at.isoformat(),
                    "context": dict(context),
                }
            )
            self._write_locked()
            return self._state

    def reset(self, operator: str) -> SystemHealth:
        """Explicit human reset back to HEALTHY (audited in history)."""
        with self._lock:
            self._state = SystemHealth.HEALTHY
            self._reason = None
            self._updated_at = datetime.now(UTC)
            self._history.append(
                {
                    "state": "HEALTHY",
                    "reason": "manual reset",
                    "operator": operator,
                    "at": self._updated_at.isoformat(),
                    "context": {},
                }
            )
            self._write_locked()
            return self._state

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "reason": self._reason,
                "updated_at": self._updated_at.isoformat(),
            }

    def _write_locked(self) -> None:
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            payload = self.to_dict()
            self.status_path.write_text(
                json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
            )
        except OSError:
            # Status file write failure must not crash a running system;
            # the in-memory state remains authoritative for this process.
            pass

    def read_status_document(self) -> dict[str, Any]:
        """Read the on-disk status (what a dashboard sees). Missing/corrupt
        files return an explicit unknown state — never assume healthy."""
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            return payload
        except (OSError, ValueError):
            return {
                "state": "unknown",
                "reason": "status file missing or unreadable",
                "updated_at": None,
            }

    def write_extended_status(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        """Merge rich operational fields (risk, reconciliation, ...) into the
        status document while keeping the health state authoritative."""
        with self._lock:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            document = {
                "generated_at": datetime.now(UTC).isoformat(),
                "system_health": self._state.value,
                "health_reason": self._reason,
                **{str(key): value for key, value in dict(fields).items()},
            }
            self.status_path.write_text(
                json.dumps(document, indent=2, default=str) + "\n", encoding="utf-8"
            )
            return document
