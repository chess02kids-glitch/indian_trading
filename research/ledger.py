"""Research ledger: hypothesis IDs and the append-only experiment record.

Every experiment gets a unique, monotonically increasing hypothesis ID
(``HYP-00001``, ``HYP-00002``, ...). Rejected strategies are recorded just
like accepted ones — the ledger is the audit trail, not a winners-only
logbook.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import Experiment, ResearchInputError

__all__ = ["HypothesisLedger", "HypothesisRecord"]

_ID_RE = re.compile(r"^HYP-(\d{5})$")


def _now() -> datetime:
    return datetime.now(UTC)


def hypothesis_id(number: int) -> str:
    """Format a 1-based hypothesis number as HYP-00001 style."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ResearchInputError("hypothesis number must be a positive integer")
    return f"HYP-{number:05d}"


def parse_hypothesis_number(hypothesis_id: str) -> int:
    match = _ID_RE.match(hypothesis_id.strip())
    if not match:
        raise ResearchInputError(
            f"hypothesis id {hypothesis_id!r} is not in HYP-00001 form"
        )
    return int(match.group(1))


class HypothesisRecord:
    """One ledger entry (accepted or rejected)."""

    FIELDS = (
        "hypothesis_id",
        "status",
        "hypothesis",
        "strategy",
        "parameters",
        "dataset_version",
        "code_commit",
        "backtest_period",
        "oos_period",
        "cost_model",
        "metrics",
        "reason",
        "recorded_at",
        "dataset_fingerprint",
        "config_fingerprint",
        "code_fingerprint",
    )

    def __init__(self, **fields: Any) -> None:
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise ResearchInputError(f"unknown ledger fields: {sorted(unknown)}")
        if not fields.get("hypothesis_id"):
            raise ResearchInputError("hypothesis_id is required")
        if fields.get("status") not in ("accepted", "rejected", "running"):
            raise ResearchInputError("status must be accepted, rejected, or running")
        self.fields = {
            "hypothesis_id": str(fields["hypothesis_id"]),
            "status": fields.get("status", "running"),
            "hypothesis": fields.get("hypothesis", ""),
            "strategy": fields.get("strategy", ""),
            "parameters": dict(fields.get("parameters") or {}),
            "dataset_version": fields.get("dataset_version"),
            "code_commit": fields.get("code_commit"),
            "backtest_period": fields.get("backtest_period"),
            "oos_period": fields.get("oos_period"),
            "cost_model": fields.get("cost_model"),
            "metrics": dict(fields.get("metrics") or {}),
            "reason": fields.get("reason"),
            "recorded_at": fields.get("recorded_at") or _now().isoformat(),
            "dataset_fingerprint": fields.get("dataset_fingerprint"),
            "config_fingerprint": fields.get("config_fingerprint"),
            "code_fingerprint": fields.get("code_fingerprint"),
        }
        
        # Enforce exact reproducibility fingerprints on accepted experiments
        if self.fields["status"] == "accepted":
            missing = [k for k in ("dataset_fingerprint", "config_fingerprint", "code_fingerprint") if not self.fields.get(k)]
            if missing:
                raise ResearchInputError(f"Accepted experiments require fingerprints for exact reproducibility: {missing}")

    def __getattr__(self, name: str) -> Any:
        if name in ("fields",):
            raise AttributeError(name)
        try:
            return self.__dict__["fields"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def to_dict(self) -> dict[str, Any]:
        return dict(self.fields)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HypothesisRecord":
        known = {key: payload.get(key) for key in cls.FIELDS}
        return cls(**known)


class HypothesisLedger:
    """Append-only JSONL ledger with a thread-safe hypothesis counter."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        # RLock: record() calls next_hypothesis_id() under the same lock.
        self._lock = threading.RLock()

    def _read_records(self) -> list[HypothesisRecord]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            records.append(HypothesisRecord.from_dict(json.loads(line)))
        return records

    def next_hypothesis_id(self) -> str:
        """Allocate the next HYP id (counter derived from existing records)."""
        with self._lock:
            highest = 0
            for record in self._read_records():
                try:
                    highest = max(
                        highest, parse_hypothesis_number(record.hypothesis_id)
                    )
                except ResearchInputError:
                    continue
            return hypothesis_id(highest + 1)

    def record(self, **fields: Any) -> HypothesisRecord:
        """Append one entry, allocating a hypothesis id when not supplied."""
        with self._lock:
            existing = {rec.hypothesis_id for rec in self._read_records()}
            if "hypothesis_id" not in fields or not fields.get("hypothesis_id"):
                fields["hypothesis_id"] = self.next_hypothesis_id()
            elif fields["hypothesis_id"] in existing:
                raise ResearchInputError(f"Duplicate hypothesis_id: {fields['hypothesis_id']}")
            record = HypothesisRecord(**fields)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record.to_dict(), sort_keys=True, default=str) + "\n"
                )
            return record

    def record_rejection(
        self,
        hypothesis: str,
        *,
        strategy: str,
        reason: str,
        metrics: Mapping[str, Any] | None = None,
        **details: Any,
    ) -> HypothesisRecord:
        """Record a rejected experiment — losers are part of the audit trail."""
        return self.record(
            status="rejected",
            hypothesis=hypothesis,
            strategy=strategy,
            reason=reason,
            metrics=metrics or {},
            **details,
        )

    def list_records(self) -> tuple[HypothesisRecord, ...]:
        with self._lock:
            return tuple(self._read_records())

    def latest(self) -> HypothesisRecord | None:
        records = self.list_records()
        return records[-1] if records else None

    def for_experiment(
        self,
        experiment: Experiment,
        *,
        status: str,
        hypothesis_text: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        reason: str | None = None,
        dataset_version: str | None = None,
        code_commit: str | None = None,
        backtest_period: str | None = None,
        oos_period: str | None = None,
        cost_model: str | None = None,
        dataset_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        code_fingerprint: str | None = None,
    ) -> HypothesisRecord:
        """Record an :class:`Experiment` outcome using its own hypothesis id."""
        return self.record(
            hypothesis_id=experiment.hypothesis_id,
            status=status,
            hypothesis=hypothesis_text or experiment.hypothesis_id,
            strategy=experiment.strategy,
            parameters=dict(experiment.parameters),
            dataset_version=dataset_version,
            code_commit=code_commit,
            backtest_period=backtest_period,
            oos_period=oos_period,
            cost_model=cost_model,
            metrics=dict(metrics or {}),
            reason=reason,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            code_fingerprint=code_fingerprint,
        )
