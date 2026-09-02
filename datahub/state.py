"""Persisted system state: heartbeats and the kill switch.

Everything the Operations page needs to answer "is this thing alive?" is written
here as a timestamped heartbeat by whichever component actually did the work.
A missing heartbeat is reported as ``never``, never as healthy.

The kill switch is a real, persisted, process-wide flag.  When it is armed:

* the paper service refuses virtual rebalances and automatic paper trading,
* the live-terminal demo bot is stopped,
* the Operations page shows a red banner and the button becomes "DISARM".

It is deliberately simple and local.  The deterministic, multi-check
:mod:`risk_kill.guard` still owns the *automatic* protective decisions; this
file is the operator's manual override.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE_ENV = "QUANT_STATE_FILE"
DEFAULT_STATE_FILE = ROOT / "var" / "system_state.json"

_lock = threading.RLock()

#: Heartbeats the Operations page renders, in display order.
HEARTBEATS = (
    "data_ingested",
    "data_bundle_refreshed",
    "signal_computed",
    "quote_refreshed",
    "paper_rebalance",
    "experiment_run",
    "reconciliation",
    "broker_ping",
)


def state_path() -> Path:
    return Path(os.getenv(STATE_FILE_ENV, str(DEFAULT_STATE_FILE)))


def _read() -> dict[str, Any]:
    path = state_path()
    if not path.is_file():
        return {"heartbeats": {}, "kill_switch": {"armed": False}, "history": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"heartbeats": {}, "kill_switch": {"armed": False}, "history": []}
    if not isinstance(payload, dict):
        return {"heartbeats": {}, "kill_switch": {"armed": False}, "history": []}
    payload.setdefault("heartbeats", {})
    payload.setdefault("kill_switch", {"armed": False})
    payload.setdefault("history", [])
    return payload


def _write(payload: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.replace(path)


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Heartbeats
# ---------------------------------------------------------------------------


def beat(name: str, detail: Any = None) -> None:
    """Record that ``name`` succeeded just now."""
    if name not in HEARTBEATS:
        raise KeyError(f"unknown heartbeat: {name}")
    with _lock:
        payload = _read()
        payload["heartbeats"][name] = {"at": _now(), "detail": detail}
        _write(payload)


def heartbeat(name: str, detail: Any = None) -> None:  # pragma: no cover - alias
    beat(name, detail)


def heartbeats() -> dict[str, dict[str, Any]]:
    with _lock:
        payload = _read()
    out: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC)
    for name in HEARTBEATS:
        entry = payload["heartbeats"].get(name)
        if not isinstance(entry, dict):
            out[name] = {"at": None, "age_seconds": None, "detail": None, "state": "never"}
            continue
        at_raw = entry.get("at")
        age: float | None = None
        try:
            age = max(0.0, (now - datetime.fromisoformat(str(at_raw))).total_seconds())
        except (TypeError, ValueError):
            at_raw = None
        state = "ok"
        if age is None:
            state = "never"
        elif age > 26 * 3600:
            state = "stale"
        out[name] = {
            "at": at_raw,
            "age_seconds": None if age is None else round(age, 1),
            "detail": entry.get("detail"),
            "state": state,
        }
    return out


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def kill_switch() -> dict[str, Any]:
    with _lock:
        payload = _read()
    switch = dict(payload["kill_switch"])
    switch.setdefault("armed", False)
    switch.setdefault("armed_at", None)
    switch.setdefault("reason", "")
    switch.setdefault("armed_by", "")
    switch["armed"] = bool(switch["armed"])
    return switch


def is_killed() -> bool:
    return bool(kill_switch()["armed"])


def set_kill_switch(armed: bool, *, reason: str = "", armed_by: str = "operator") -> dict[str, Any]:
    with _lock:
        payload = _read()
        switch = {
            "armed": bool(armed),
            "armed_at": _now() if armed else payload.get("kill_switch", {}).get("armed_at"),
            "reason": reason,
            "armed_by": armed_by,
            "disarmed_at": None if armed else _now(),
        }
        payload["kill_switch"] = switch
        history = list(payload.get("history") or [])
        history.append(
            {
                "at": _now(),
                "action": "KILL_SWITCH_ARMED" if armed else "KILL_SWITCH_DISARMED",
                "reason": reason,
                "by": armed_by,
            }
        )
        payload["history"] = history[-200:]
        _write(payload)
    return kill_switch()


def history(limit: int = 25) -> list[dict[str, Any]]:
    with _lock:
        payload = _read()
    return list(payload.get("history") or [])[-limit:][::-1]


# ---------------------------------------------------------------------------
# Generic persisted state (added for AUDIT-021)
# ---------------------------------------------------------------------------


def append_history(*, action: str, reason: str = "", by: str = "system") -> None:
    """Append one entry to the persisted audit history.

    Public so that :mod:`datahub.kill_switch` can record refusals without
    reaching into this module's private helpers.
    """
    with _lock:
        payload = _read()
        history = list(payload.get("history") or [])
        history.append(
            {
                "at": _now(),
                "action": action,
                "reason": reason,
                "by": by,
            }
        )
        payload["history"] = history[-200:]
        _write(payload)


def set_state_value(key: str, detail: Any) -> None:
    """Persist an arbitrary timestamped value under ``heartbeats[key]``.

    Used for values that are not heartbeats in the Operations-page sense but
    must still survive a restart (for example the last automatic protective
    risk state).
    """
    with _lock:
        payload = _read()
        payload.setdefault("heartbeats", {})[key] = {"at": _now(), "detail": detail}
        _write(payload)


def get_state_value(key: str) -> Any | None:
    """Read a value written by :func:`set_state_value` (``None`` when absent)."""
    with _lock:
        payload = _read()
    entry = payload.get("heartbeats", {}).get(key)
    return entry if isinstance(entry, dict) else None
