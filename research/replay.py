"""Deterministic long-run replay tooling for research and paper stress tests.

A ``LongRunReplay`` drives a *caller-supplied* step function over a
deterministic multi-month schedule:

* **deterministic scheduling** — the schedule derives only from dates,
  frequencies, and the seed (no wall clock, no randomness);
* **restart recovery** — after every step the replay state is persisted, so
  a killed process resumes exactly where it stopped;
* **duplicate scheduler protection** — a step can only be *claimed* once
  (atomic lock + persisted state); a duplicate claim is reported and
  skipped, never re-executed.

The replay is research-side tooling: it never imports execution, broker, or
risk modules and never creates orders. The step function itself is supplied
by the caller and may be a paper pipeline or a pure simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from research.contracts import ResearchInputError

__all__ = ["LongRunReplay", "ReplayOutcome", "ReplaySchedule"]


@dataclass(frozen=True, slots=True)
class ReplaySchedule:
    """Deterministic schedule of replay steps."""

    replay_id: str
    start: date
    end: date
    frequency: str
    rebalance_frequency: str
    steps: tuple[date, ...]
    seed: int

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable schedule description."""
        return {
            "replay_id": self.replay_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "frequency": self.frequency,
            "rebalance_frequency": self.rebalance_frequency,
            "seed": self.seed,
            "steps": [step.isoformat() for step in self.steps],
        }

    @property
    def config_checksum(self) -> str:
        """Stable fingerprint of the schedule configuration."""
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, default=str, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """Result of one replay run (completed, skipped as duplicate, or failed)."""

    replay_id: str
    completed: tuple[date, ...]
    skipped_duplicates: tuple[date, ...]
    failed: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable outcome."""
        return {
            "replay_id": self.replay_id,
            "completed": [step.isoformat() for step in self.completed],
            "skipped_duplicates": [
                step.isoformat() for step in self.skipped_duplicates
            ],
            "failed": dict(self.failed),
        }


class LongRunReplay:
    """Stateful, restart-safe replay driver (one instance per replay id).

    The state file is JSON beneath ``state_dir``; writes are atomic
    (temp file + ``os.replace``), so a crash can never corrupt the resume
    point. Step claims use an exclusive lock directory so two schedulers can
    never execute the same step concurrently.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        replay_id: str,
        start: date,
        end: date,
        frequency: str = "B",
        rebalance_frequency: str = "M",
        seed: int = 42,
        lock_timeout: float = 3600.0,
    ) -> None:
        if not replay_id.strip() or "/" in replay_id or "\\" in replay_id:
            raise ResearchInputError("replay_id must be a non-empty safe string")
        if end < start:
            raise ResearchInputError("end must not precede start")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ResearchInputError("seed must be an integer")
        self.state_dir = Path(state_dir).expanduser()
        self.replay_id = replay_id
        self.start = start
        self.end = end
        self.frequency = frequency
        self.rebalance_frequency = rebalance_frequency
        self.seed = seed
        self.state_path = self.state_dir / f"{replay_id}.json"
        self._lock_dir = self.state_dir / f"{replay_id}.lock"
        self.lock_timeout = float(lock_timeout)

    # -- scheduling -----------------------------------------------------------

    def build_schedule(self) -> ReplaySchedule:
        """Build the deterministic step schedule for this replay window.

        Steps are the trading dates between ``start`` and ``end`` (inclusive)
        at ``frequency`` (default business days). The schedule is a pure
        function of the constructor arguments.
        """
        try:
            index = pd.date_range(
                start=self.start,
                end=self.end,
                freq=self.frequency,
            )
        except ValueError as exc:
            raise ResearchInputError(
                f"invalid replay frequency {self.frequency!r}"
            ) from exc
        steps = tuple(date.fromisoformat(value.date().isoformat()) for value in index)
        if not steps:
            raise ResearchInputError("replay schedule contains no steps")
        return ReplaySchedule(
            replay_id=self.replay_id,
            start=self.start,
            end=self.end,
            frequency=self.frequency,
            rebalance_frequency=self.rebalance_frequency,
            steps=steps,
            seed=self.seed,
        )

    # -- state ----------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"replay_id": self.replay_id, "steps": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchInputError(
                f"replay state {self.state_path} is unreadable: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("replay_id") != self.replay_id:
            raise ResearchInputError("replay state does not belong to this replay id")
        return payload

    def _save_state(self, state: Mapping[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.state_dir, prefix=f".{self.replay_id}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    dict(state),
                    handle,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                )
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def completed_steps(self) -> tuple[date, ...]:
        """Return the ordered list of steps already completed."""
        state = self._load_state()
        steps = state.get("steps", {})
        return tuple(
            date.fromisoformat(step)
            for step in sorted(
                key
                for key, entry in steps.items()
                if entry.get("status") == "completed"
            )
        )

    # -- claim / run -----------------------------------------------------------

    def _claim_step(self, step: date) -> str | None:
        """Atomically claim one step; returns a claim token or None.

        The lock directory is exclusive by construction (``mkdir`` is
        atomic on POSIX), so two schedulers can never claim the same step.
        A lock whose owner process is dead (or which is older than
        ``lock_timeout`` seconds — e.g. after a machine crash) is reclaimed
        so restart recovery can proceed.
        """
        lock_path = self._lock_dir / step.isoformat()
        token = hashlib.sha256(
            f"{self.replay_id}|{step.isoformat()}".encode()
        ).hexdigest()[:16]
        for attempt in range(2):
            try:
                lock_path.mkdir(parents=True)
            except FileExistsError:
                if attempt == 0 and self._stale_lock(lock_path):
                    import shutil

                    shutil.rmtree(lock_path, ignore_errors=True)
                    continue
                return None
            try:
                (lock_path / "claim.json").write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "token": token,
                            "claimed_at": datetime.now(UTC).isoformat(),
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                return None
            return token
        return None

    def _stale_lock(self, lock_path: Path) -> bool:
        """Return True when a step lock belongs to a dead or ancient owner."""
        claim_file = lock_path / "claim.json"
        try:
            payload = json.loads(claim_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Lock exists but cannot be inspected: treat as stale after the
            # timeout so restart recovery cannot be permanently blocked.
            return self._lock_age(lock_path) > self.lock_timeout
        pid = int(payload.get("pid", -1))
        if pid > 0:
            try:
                os.kill(pid, 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
        return self._lock_age(lock_path) > self.lock_timeout

    @staticmethod
    def _lock_age(lock_path: Path) -> float:
        try:
            return datetime.now(UTC).timestamp() - lock_path.stat().st_mtime
        except OSError:
            return 0.0

    def _release_step(self, step: date, token: str | None) -> None:
        if token is None:
            return
        lock_path = self._lock_dir / step.isoformat()
        import shutil

        shutil.rmtree(lock_path, ignore_errors=True)

    def run(
        self,
        step_fn: Callable[[date, ReplaySchedule, int], Any],
        *,
        on_error: Callable[[date, Exception], Any] | None = None,
    ) -> ReplayOutcome:
        """Execute every pending schedule step, recovering from prior runs.

        ``step_fn(step_date, schedule, attempt)`` is called once per pending
        step; completed steps are skipped and reported as duplicate
        attempts. When ``step_fn`` raises, the step is recorded as ``failed``
        in state and the error is reported through ``on_error`` (the run
        continues with subsequent steps).
        """
        schedule = self.build_schedule()
        state = self._load_state()
        if state.get("config_checksum") not in (None, schedule.config_checksum):
            raise ResearchInputError(
                "replay configuration changed since the last run — "
                "refusing to resume a different schedule"
            )
        state["config_checksum"] = schedule.config_checksum
        steps_state = state.setdefault("steps", {})
        completed: list[date] = []
        skipped: list[date] = []
        failed: dict[str, str] = {}
        for step in schedule.steps:
            key = step.isoformat()
            entry = steps_state.get(key, {})
            if entry.get("status") == "completed":
                skipped.append(step)
                continue
            token = self._claim_step(step)
            if token is None:
                skipped.append(step)
                continue
            try:
                attempt = int(entry.get("attempts", 0)) + 1
                step_fn(step, schedule, attempt)
                steps_state[key] = {
                    "status": "completed",
                    "attempts": attempt,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
                self._save_state(state)
                completed.append(step)
            except Exception as exc:  # noqa: BLE001 - failures are recorded
                steps_state[key] = {
                    "status": "failed",
                    "attempts": int(entry.get("attempts", 0)) + 1,
                    "error": str(exc),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                self._save_state(state)
                failed[key] = str(exc)
                if on_error is not None:
                    on_error(step, exc)
            finally:
                self._release_step(step, token)
        return ReplayOutcome(
            replay_id=self.replay_id,
            completed=tuple(completed),
            skipped_duplicates=tuple(skipped),
            failed=failed,
        )
