"""Execution-mode feature flags for the broker layer.

The operating modes are:

* ``RESEARCH`` — research only; no broker interaction of any kind.
* ``PAPER`` — deterministic paper trading via :class:`execution.paper.PaperBroker`.
* ``SANDBOX`` — broker round-trips against sandbox environments only.
* ``LIVE`` — **permanently disabled**. It exists so that refusal is an
  explicit, testable code path rather than an absent enum value. Any attempt
  to execute in ``LIVE`` mode raises :class:`LiveTradingDisabledError` and
  :func:`check_execution_permitted` refuses it before any adapter is reached.

The domain-level :class:`models.domain.ExecutionMode` deliberately has no
``LIVE`` member (enforced by ``tests.test_architecture``); this module is the
single place where the disabled ``LIVE`` concept is materialised and refused.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Mapping

from broker.errors import LiveTradingDisabledError
from models.domain import ExecutionMode

__all__ = [
    "OperatingMode",
    "EXECUTION_MODE_ENV",
    "check_execution_permitted",
    "resolve_operating_mode",
    "to_execution_mode",
]

#: Environment variable selecting the operating mode.
EXECUTION_MODE_ENV = "QUANT_EXECUTION_MODE"


class OperatingMode(str, Enum):
    """Broker-layer operating modes, including the disabled ``LIVE`` mode."""

    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SANDBOX = "SANDBOX"
    LIVE = "LIVE"

    @property
    def execution_permitted(self) -> bool:
        """True only for modes that may submit orders (PAPER/SANDBOX)."""
        return self in (OperatingMode.PAPER, OperatingMode.SANDBOX)


def resolve_operating_mode(
    environ: Mapping[str, str] | None = None,
    *,
    default: OperatingMode = OperatingMode.RESEARCH,
) -> OperatingMode:
    """Resolve the configured operating mode.

    Reads ``QUANT_EXECUTION_MODE`` (default ``RESEARCH`` — fail closed).
    Unknown values raise ``ValueError``; ``LIVE`` resolves successfully so
    callers can refuse it explicitly via :func:`check_execution_permitted`.
    """
    source = os.environ if environ is None else environ
    raw = source.get(EXECUTION_MODE_ENV)
    if raw is None or not raw.strip():
        return default
    try:
        return OperatingMode(raw.strip().upper())
    except ValueError as exc:
        permitted = ", ".join(member.value for member in OperatingMode)
        raise ValueError(
            f"invalid {EXECUTION_MODE_ENV}={raw!r}; expected one of {permitted}"
        ) from exc


def to_execution_mode(mode: OperatingMode) -> ExecutionMode:
    """Map an operating mode onto the domain ``ExecutionMode``.

    ``LIVE`` has no domain counterpart by design; requesting it raises
    :class:`LiveTradingDisabledError`.
    """
    if mode is OperatingMode.LIVE:
        raise LiveTradingDisabledError(
            "LIVE execution is disabled: there is no live order path in this "
            "system. Use SANDBOX for broker round-trips."
        )
    return ExecutionMode(mode.value)


def check_execution_permitted(mode: OperatingMode | str) -> ExecutionMode:
    """Refuse execution unless the mode is PAPER or SANDBOX.

    This is a hard gate placed in front of every broker submission path:

    * ``LIVE`` → :class:`LiveTradingDisabledError` (always).
    * ``RESEARCH`` → :class:`LiveTradingDisabledError` (no trading context).
    * ``PAPER`` / ``SANDBOX`` → the corresponding domain ``ExecutionMode``.
    """
    if not isinstance(mode, OperatingMode):
        try:
            mode = OperatingMode(str(mode).strip().upper())
        except ValueError as exc:
            raise LiveTradingDisabledError(
                f"unknown operating mode {mode!r}; execution refused"
            ) from exc
    if mode is OperatingMode.LIVE:
        raise LiveTradingDisabledError(
            "LIVE execution is disabled by policy; refusing to submit any order"
        )
    if mode is OperatingMode.RESEARCH:
        raise LiveTradingDisabledError("RESEARCH mode does not permit order execution")
    return ExecutionMode(mode.value)
