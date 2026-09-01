"""Deterministic mapping from :class:`risk_kill.guard.RiskState` to operations.

``risk_kill`` owns the *protective* vocabulary (``STOP_NEW_ORDERS``,
``FLATTEN_POSITIONS``, ``LOCK_ACCOUNT`` ...).  ``observability.health`` owns the
*reporting* vocabulary (``WARNING`` / ``HALTED`` / ``LOCKED``).  They are
different enums and must never be conflated: ``execution.service`` used to
compare a ``RiskState`` against ``RiskState.HALTED`` / ``RiskState.LOCKED`` /
``RiskState.WARNING``, none of which exist, so every protective decision raised
``AttributeError`` out of the fail-closed path instead of halting the run.

This module is the single place where the two vocabularies meet.  It returns
the *name* of the target ``SystemHealth`` member (never the enum object) so
that :mod:`risk_kill` keeps its hard architectural rule — standard library only,
no import of ``observability``, ``execution``, ``broker``, or anything else.

The mapping is deliberately **fail-closed and total**:

* an unknown/None state maps to the most severe health state (``LOCKED``), and
* any state that forbids new orders is a hard halt (critical alert).
"""

from __future__ import annotations

from typing import Any

from .guard import RiskState

__all__ = [
    "HEALTH_FOR_RISK_STATE",
    "HARD_HALT_STATES",
    "health_name_for_risk_state",
    "is_hard_halt",
]

#: Risk states that must raise a *critical* alert (capital is at risk / the
#: account is locked).  Everything else protective is a warning.
HARD_HALT_STATES = frozenset(
    {
        RiskState.LOCK_ACCOUNT,
        RiskState.FLATTEN_POSITIONS,
        RiskState.CANCEL_OPEN_ORDERS,
    }
)

#: The one and only RiskState -> SystemHealth *name* mapping.
HEALTH_FOR_RISK_STATE: dict[RiskState, str] = {
    RiskState.NOMINAL: "HEALTHY",
    # ALERT_HUMAN: nothing protective fired, but a human must look at it.
    RiskState.ALERT_HUMAN: "WARNING",
    RiskState.STOP_NEW_ORDERS: "HALTED",
    RiskState.CANCEL_OPEN_ORDERS: "HALTED",
    RiskState.FLATTEN_POSITIONS: "HALTED",
    RiskState.LOCK_ACCOUNT: "LOCKED",
}


def _coerce(state: Any) -> RiskState | None:
    """Best-effort coercion of ``state`` to a :class:`RiskState`."""
    if isinstance(state, RiskState):
        return state
    if isinstance(state, str):
        try:
            return RiskState(state.strip().upper())
        except ValueError:
            return None
    return None


def health_name_for_risk_state(state: Any) -> str:
    """Return the ``SystemHealth`` *member name* for a protective state.

    Unknown, missing, or unparseable states return ``"LOCKED"`` — the most
    severe health state — so a bug in the mapping can never under-report risk.
    """
    resolved = _coerce(state)
    if resolved is None:
        return "LOCKED"
    return HEALTH_FOR_RISK_STATE.get(resolved, "LOCKED")


def is_hard_halt(state: Any) -> bool:
    """True when the protective state warrants a critical (not warning) alert."""
    resolved = _coerce(state)
    if resolved is None:
        # An unrecognised protective state is treated as the worst case.
        return True
    return resolved is not RiskState.NOMINAL and resolved not in (RiskState.ALERT_HUMAN,)
