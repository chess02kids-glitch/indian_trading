"""Rate limiter pacing/queueing and exponential retry behaviour."""

from __future__ import annotations

import threading

import pytest

from broker.errors import BrokerError, BrokerTransportError
from broker.rate_limit import RateLimiter, call_with_retries
from tests.sandbox_common import FakeMono, SleepLog


class TestRateLimiter:
    def test_default_is_one_per_second(self) -> None:
        limiter = RateLimiter()
        assert limiter.calls_per_second == 1.0
        assert limiter.interval == 1.0

    def test_first_call_immediate_second_paced(self) -> None:
        mono = FakeMono()
        sleeps = SleepLog()
        limiter = RateLimiter(1.0, clock=mono, sleep=sleeps)
        assert limiter.acquire() == 0.0
        # clock hasn't advanced: second acquisition must wait one interval
        assert limiter.acquire() == 1.0
        assert sleeps.calls == [1.0]
        assert limiter.acquire() == 2.0
        assert sleeps.calls == [1.0, 2.0]

    def test_no_wait_after_interval_passes(self) -> None:
        mono = FakeMono()
        sleeps = SleepLog()
        limiter = RateLimiter(1.0, clock=mono, sleep=sleeps)
        limiter.acquire()
        mono.advance(2.5)
        assert limiter.acquire() == 0.0
        assert sleeps.calls == []

    def test_peek_wait_does_not_reserve(self) -> None:
        mono = FakeMono()
        limiter = RateLimiter(1.0, clock=mono, sleep=SleepLog())
        first = limiter.peek_wait()
        second = limiter.peek_wait()
        assert first == 0.0 and second == 0.0

    def test_invalid_rate_rejected(self) -> None:
        with pytest.raises(BrokerError):
            RateLimiter(0.0)
        with pytest.raises(BrokerError):
            RateLimiter(-2)

    def test_concurrent_callers_are_queued_in_order(self) -> None:
        """Threads queue: slots are spaced by the interval, never bunched."""
        mono = FakeMono()

        def _gate_sleep(seconds: float) -> None:
            mono.advance(seconds)

        limiter = RateLimiter(5.0, clock=mono, sleep=_gate_sleep)
        barrier = threading.Barrier(4)
        slots: list[float] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            limiter.acquire()
            with lock:
                slots.append(mono.value)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ordered = sorted(slots)
        for earlier, later in zip(ordered, ordered[1:]):
            assert later - earlier >= 0.2 - 1e-9


class TestCallWithRetries:
    def test_succeeds_first_try(self) -> None:
        calls: list[int] = []
        result = call_with_retries(lambda: calls.append(1) or "ok")
        assert result == "ok"
        assert calls == [1]

    def test_retries_retryable_then_succeeds(self) -> None:
        sleeps = SleepLog()
        attempts: list[int] = []

        def flaky() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise BrokerTransportError("timeout")
            return "recovered"

        result = call_with_retries(flaky, max_attempts=3, base_delay=0.5, sleep=sleeps)
        assert result == "recovered"
        assert len(attempts) == 3
        # exponential backoff: 0.5 then 1.0
        assert sleeps.calls == [0.5, 1.0]

    def test_non_retryable_propagates_immediately(self) -> None:
        attempts: list[int] = []

        def bad() -> None:
            attempts.append(1)
            raise ValueError("not retryable")

        with pytest.raises(ValueError):
            call_with_retries(bad, max_attempts=5)
        assert attempts == [1]

    def test_exhaustion_raises_transport_error(self) -> None:
        sleeps = SleepLog()

        def always_fails() -> None:
            raise BrokerTransportError("still down")

        with pytest.raises(BrokerTransportError, match="after 2 attempt"):
            call_with_retries(
                always_fails, max_attempts=2, base_delay=0.25, sleep=sleeps
            )
        assert sleeps.calls == [0.25]

    def test_max_attempts_guard(self) -> None:
        with pytest.raises(BrokerError):
            call_with_retries(lambda: None, max_attempts=0)
