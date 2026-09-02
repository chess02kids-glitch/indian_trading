"""The one authority for "is the operator kill switch armed?".

AUDIT-021
=========

Before this module existed the operator kill switch lived in
:mod:`datahub.state` (a persisted, process-wide flag) but was consulted by
only two callers:

* :class:`paper_trading.service.PaperTradingService` (rebalances and
  automation), and
* the live-terminal demo bot.

``ExecutionService.execute_targets`` — the function that actually turns a
:class:`~models.domain.PortfolioTarget` into orders — and
``DailyPipeline.run_day`` — the scheduled entry point — **never looked at it**.
An operator who pressed ARM in the Operations page stopped the paper page and
nothing else; every orchestrated run carried on submitting orders. Worse, the
automatic protective layer (:class:`risk_kill.guard.RiskGuard`) has no
persistence at all, so a protective ``LOCK_ACCOUNT`` evaporated on restart and
the next process started from ``NOMINAL``.

Design
------

``datahub.state`` is the single source of truth — the switch is *persisted*,
so it survives a restart, and it is readable from a different process than the
one that armed it. This module is the only place that reads it for an
authorisation decision, so there is exactly one rule and one audit trail.

It also records the last automatic protective state
(``heartbeats.risk_state``) so a restart can see that the previous process
locked the account, and it exposes
:func:`restore_risk_state` so a fresh process can re-apply it.

Everything here fails **closed**: if the state file cannot be read or is
corrupt, :func:`is_killed` returns ``True``. A corrupt kill-switch file must
never be interpreted as permission to trade.

Nothing in this module may raise: it is called from inside execution and
orchestration paths where an exception would be caught by a broad handler and
silently turned into "carry on".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "KILL_SWITCH_HISTORY_ACTION",
    "RISK_STATE_CLEARED_ACTION",
    "blocked_reason",
    "clear_risk_state",
    "is_killed",
    "note_blocked",
    "record_risk_state",
    "require_not_killed",
    "restore_risk_state",
]

#: Heartbeat key under which the last automatic protective state is persisted.
RISK_STATE_KEY = "risk_state"

#: History action recorded every time the switch refuses an order-creating path.
KILL_SWITCH_HISTORY_ACTION = "KILL_SWITCH_BLOCKED"

#: History action recorded when an operator clears a protective risk latch.
RISK_STATE_CLEARED_ACTION = "RISK_STATE_CLEARED"


def _state_module() -> Any:
    """Import :mod:`datahub.state` lazily (keeps ``datahub`` optional here)."""
    from datahub import state

    return state


def _switch() -> dict[str, Any]:
    try:
        return _state_module().kill_switch()
    except Exception:  # noqa: BLE001 - fail closed, never raise from a guard
        logger.exception("kill_switch_read_failed_failing_closed")
        return {"armed": True, "armed_at": None, "reason": "state file unreadable"}


def is_killed() -> bool:
    """Return ``True`` when the operator kill switch is armed (or unreadable)."""
    try:
        return bool(_switch()["armed"])
    except Exception:  # noqa: BLE001 - fail closed
        logger.exception("kill_switch_read_failed_failing_closed")
        return True


def blocked_reason() -> str:
    """Human-readable reason the switch is armed (empty when it is not)."""
    if not is_killed():
        return ""
    switch = _switch()
    reason = str(switch.get("reason") or "").strip()
    armed_at = switch.get("armed_at")
    armed_by = str(switch.get("armed_by") or "").strip()
    parts = [part for part in (reason, f"armed by {armed_by}" if armed_by else "") if part]
    suffix = f" ({'; '.join(parts)})" if parts else ""
    if armed_at:
        suffix += f" at {armed_at}"
    return f"operator kill switch is armed{suffix}"


def note_blocked(action: str) -> None:
    """Record that ``action`` was refused, so the refusal is auditable."""
    try:
        _state_module().append_history(
            action=KILL_SWITCH_HISTORY_ACTION,
            reason=f"{action} blocked by the armed kill switch",
            by="system",
        )
    except Exception:  # noqa: BLE001 - auditing must never break the guard
        logger.exception("kill_switch_audit_failed action=%s", action)


def require_not_killed(action: str) -> bool:
    """Return ``True`` when ``action`` must be refused.

    Called by every order-creating path. Returns ``True`` (refuse) when the
    switch is armed **or** when the state file cannot be read. Never raises.
    """
    if not is_killed():
        return False
    logger.warning("kill_switch_blocked action=%s", action)
    note_blocked(action)
    return True


# ---------------------------------------------------------------------------
# Automatic protective state (risk_kill) persistence
# ---------------------------------------------------------------------------


def record_risk_state(risk_state: str, *, reason: str = "") -> None:
    """Persist the last automatic protective state so a restart can see it.

    ``risk_kill.guard.RiskGuard`` is a pure in-memory object: a
    ``LOCK_ACCOUNT`` decision died with the process that produced it. Writing
    it here means the next process can re-apply it instead of starting from
    ``NOMINAL``.
    """
    if not risk_state:
        return
    try:
        _state_module().set_state_value(
            RISK_STATE_KEY, {"risk_state": risk_state, "reason": reason}
        )
    except Exception:  # noqa: BLE001 - persistence must never break a halt
        logger.exception("risk_state_persist_failed state=%s", risk_state)


def clear_risk_state(*, by: str = "system") -> None:
    """Explicitly forget the last automatic protective state.

    An operator-only action (and an audited one). It exists so a protective
    latch can be released without editing files by hand.
    """
    try:
        state = _state_module()
        state.set_state_value(
            RISK_STATE_KEY, {"risk_state": "NOMINAL", "reason": f"cleared by {by}"}
        )
        state.append_history(
            action=RISK_STATE_CLEARED_ACTION,
            reason="persisted protective risk state cleared",
            by=by,
        )
    except Exception:  # noqa: BLE001
        logger.exception("risk_state_clear_failed")


def restore_risk_state(*, consume: bool = True) -> str | None:
    """Return the last persisted automatic protective state, if any.

    ``consume=True`` (the default) clears the latch as it is read, so a
    restart halts **at least once** with a critical alert and the operator
    then gets a fresh risk-guard evaluation instead of a permanent lock.
    This is deliberately weaker than an operator-acknowledged latch: the
    guard remains the authority for ongoing protection, and
    :func:`clear_risk_state` is the explicit release.

    Returns ``None`` when nothing protective was recorded, or when the value
    is ``NOMINAL``.
    """
    try:
        entry = _state_module().get_state_value(RISK_STATE_KEY)
    except Exception:  # noqa: BLE001
        logger.exception("risk_state_restore_failed")
        return None
    if not isinstance(entry, dict):
        return None
    detail = entry.get("detail")
    if not isinstance(detail, dict):
        return None
    value = detail.get("risk_state")
    if not value or str(value) == "NOMINAL":
        return None
    if consume:
        try:
            _state_module().set_state_value(
                RISK_STATE_KEY,
                {"risk_state": "NOMINAL", "reason": "consumed on restart"},
            )
        except Exception:  # noqa: BLE001
            logger.exception("risk_state_consume_failed")
    return str(value)
