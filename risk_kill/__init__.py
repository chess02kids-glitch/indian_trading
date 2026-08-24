"""Safety kill-switch package — zero AI imports.

Public API:

* :class:`RiskState` — the protective states (incl. NOMINAL steady state).
* :class:`RiskLimits` — explicit risk thresholds.
* :class:`RiskContext` — plain-data snapshot of trading state.
* :class:`RiskGuard` — deterministic evaluation of context against limits.
* :class:`RiskDecision` — deterministic guard output.

Architecture rule (enforced by ``tests/test_architecture.py``): this package
imports nothing outside the Python standard library. AI/agent code can never
influence kill-switch decisions.
"""

from .guard import (
    RISK_RESPONSES,
    RiskCheckError,
    RiskContext,
    RiskDecision,
    RiskGuard,
    RiskLimits,
    RiskState,
    worst_state,
)

__all__ = [
    "RISK_RESPONSES",
    "RiskCheckError",
    "RiskContext",
    "RiskDecision",
    "RiskGuard",
    "RiskLimits",
    "RiskState",
    "worst_state",
]
