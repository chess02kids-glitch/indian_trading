"""Conservative client-side rate limiting and retry with exponential backoff.

Broker APIs (even sandboxes) rate-limit aggressively, and bursts are how
duplicate orders happen in real systems. Defaults are deliberately
conservative: **1 order per second**, FIFO-paced through :meth:`acquire`.

Both the clock and the sleeper are injectable so tests are deterministic and
instant — no test ever waits a real second.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

from broker.errors import BrokerError, BrokerTransportError

__all__ = ["RateLimiter", "RetryExhaustedError", "call_with_retries"]

T = TypeVar("T")


class RetryExhaustedError(BrokerError):
    """Raised when a retryable operation exhausts its attempts."""


class RateLimiter:
    """Pacing limiter: serialises callers at most every ``interval`` seconds.

    Callers queue on an internal lock; each caller reserves the next time
    slot and sleeps until it arrives, so the effective rate can never exceed
    the configured limit and concurrent submissions stay in FIFO order.
    """

    def __init__(
        self,
        calls_per_second: float = 1.0,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if calls_per_second <= 0:
            raise BrokerError("calls_per_second must be positive")
        self.calls_per_second = float(calls_per_second)
        self.interval = 1.0 / self.calls_per_second
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next_slot = self._clock()

    def acquire(self) -> float:
        """Wait for the next slot; returns the seconds waited.

        The slot is reserved under the lock before sleeping, so racing
        callers are spaced deterministically (queued, not stamped).
        """
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_slot - now)
            start = now + wait
            self._next_slot = max(self._next_slot, start) + self.interval
        if wait > 0:
            self._sleep(wait)
        return wait

    def peek_wait(self) -> float:
        """Seconds until the next free slot, without reserving it."""
        with self._lock:
            return max(0.0, self._next_slot - self._clock())


def call_with_retries(
    operation: Callable[[], T],
    *,
    retryable: tuple[type[BaseException], ...] = (BrokerTransportError,),
    max_attempts: int = 3,
    base_delay: float = 0.25,
    sleep: Callable[[float], None] | None = None,
    describe: str = "broker call",
) -> T:
    """Run ``operation`` with exponential backoff on retryable failures.

    Delays are ``base_delay * 2**attempt`` (0.25s, 0.5s, 1s, ...). Only
    exceptions in ``retryable`` are retried; everything else propagates
    immediately. On exhaustion raises the last retryable exception (never
    silently succeeds).
    """
    if max_attempts < 1:
        raise BrokerError("max_attempts must be at least 1")
    sleeper = sleep or time.sleep
    last_error: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return operation()
        except retryable as exc:  # noqa: PERF203 - intentional narrow retry
            last_error = exc
            if attempt == max_attempts - 1:
                break
            sleeper(base_delay * (2**attempt))
    raise BrokerTransportError(
        f"{describe} failed after {max_attempts} attempt(s): {last_error}"
    ) from last_error
