"""Deterministic idempotency for order submission.

The same logical order must always produce the same idempotency key, and a
retry (timeout, crash recovery, duplicate signal) must never create a second
order. Key generation is a pure function of the logical order fields; the
:class:`IdempotencyRegistry` tracks first-seen keys and rejects duplicates.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "IdempotencyRegistry",
    "IdempotencyResult",
    "compute_idempotency_key",
]

#: Fields that define a logical order. Everything else (timestamps of
#: submission attempts, request ids) must NOT influence the key.
_KEY_FIELDS = (
    "strategy_id",
    "hypothesis_id",
    "symbol",
    "side",
    "quantity",
    "limit_price",
    "order_type",
    "rebalance_date",
)


def compute_idempotency_key(fields: Mapping[str, Any]) -> str:
    """Return the stable key for one logical order.

    ``fields`` must contain every field in :data:`_KEY_FIELDS`. Values are
    normalized (strings upper-cased/stripped for identifiers and enums) so
    that logically identical orders hash identically.
    """
    missing = set(_KEY_FIELDS) - set(fields)
    if missing:
        raise ValueError(f"idempotency fields missing: {sorted(missing)}")
    payload = {}
    for name in _KEY_FIELDS:
        value = fields[name]
        if isinstance(value, str):
            value = (
                value.strip().upper()
                if name
                in (
                    "symbol",
                    "side",
                    "order_type",
                    "strategy_id",
                    "hypothesis_id",
                )
                else value.strip()
            )
        if name == "rebalance_date" and value is not None:
            value = str(value)
        if name in ("quantity", "limit_price"):
            value = float(value)
        payload[name] = value
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyResult:
    """Outcome of an idempotency-registry claim."""

    accepted: bool
    key: str
    reason: str


class IdempotencyRegistry:
    """Thread-safe first-seen tracking for idempotency keys.

    Semantics:

    * The first claim of a key is accepted.
    * A repeated claim for an in-flight key is rejected (duplicate).
    * After the logical order completes (``mark_completed``), the same key is
      rejected as an already-completed duplicate — a retry of a completed
      order must not create a new order either.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key -> True once completed; False while in flight
        self._states: dict[str, bool] = {}

    def claim(self, key: str) -> IdempotencyResult:
        if not isinstance(key, str) or not key.strip():
            return IdempotencyResult(False, key or "", "empty idempotency key")
        with self._lock:
            state = self._states.get(key)
            if state is None:
                self._states[key] = False
                return IdempotencyResult(True, key, "first seen")
            if state:
                return IdempotencyResult(False, key, "already completed")
            return IdempotencyResult(False, key, "in flight")

    def mark_completed(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                self._states[key] = True

    def accepted_keys(self) -> dict[str, bool]:
        """Snapshot of key -> completed, usable by the risk guard duplicate check."""
        with self._lock:
            return dict(self._states)

    def clear(self) -> None:
        with self._lock:
            self._states.clear()
