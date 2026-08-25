"""Research ledger: hypothesis IDs and the append-only experiment record.

Every experiment gets a unique, monotonically increasing hypothesis ID
(``HYP-00001``, ``HYP-00002``, ...). Successful, rejected, failed, and
interrupted runs are recorded just like accepted ones — the ledger is the
audit trail, not a winners-only logbook. The ledger is immutable and
append-only: records are never updated or deleted, and duplicate research
fingerprints are detected and reported (or rejected) explicitly so
survivorship bias can never be introduced by silent re-runs.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import Experiment, ResearchInputError

__all__ = [
    "DuplicateExperimentError",
    "HypothesisLedger",
    "HypothesisRecord",
    "LEDGER_STATUSES",
    "parse_hypothesis_number",
    "hypothesis_id",
]

_ID_RE = re.compile(r"^HYP-(\d{5})$")

#: Statuses a first-class research record may carry.
LEDGER_STATUSES = (
    "accepted",
    "rejected",
    "running",
    "failed",
    "interrupted",
    "halted",
    "invalid",
    "insufficient_data",
    "duplicate",
    "abandoned",
)


class DuplicateExperimentError(ResearchInputError):
    """Raised when a duplicate research fingerprint is explicitly rejected."""


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
    """One immutable ledger entry: accepted, rejected, failed, or interrupted."""

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
        "holdout_period",
        "universe_version",
        "cost_model",
        "metrics",
        "reason",
        "recorded_at",
        "dataset_fingerprint",
        "config_fingerprint",
        "code_fingerprint",
        "experiment_id",
        "run_id",
        "gate_result",
        "is_duplicate",
        "duplicate_of",
        "parent_hypothesis_id",
        "campaign_id",
        "strategy_family",
        "strategy_version",
        "feature_set_hash",
        "parameter_hash",
        "validation_protocol_version",
    )

    def __init__(self, **fields: Any) -> None:
        unknown = set(fields) - set(self.FIELDS)
        if unknown:
            raise ResearchInputError(f"unknown ledger fields: {sorted(unknown)}")
        if not fields.get("hypothesis_id"):
            raise ResearchInputError("hypothesis_id is required")
        if fields.get("status") not in LEDGER_STATUSES:
            raise ResearchInputError(
                "status must be one of: " + ", ".join(LEDGER_STATUSES)
            )
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
            "holdout_period": fields.get("holdout_period"),
            "universe_version": fields.get("universe_version"),
            "cost_model": fields.get("cost_model"),
            "metrics": dict(fields.get("metrics") or {}),
            "reason": fields.get("reason"),
            "recorded_at": fields.get("recorded_at") or _now().isoformat(),
            "dataset_fingerprint": fields.get("dataset_fingerprint"),
            "config_fingerprint": fields.get("config_fingerprint"),
            "code_fingerprint": fields.get("code_fingerprint"),
            "experiment_id": fields.get("experiment_id"),
            "run_id": fields.get("run_id"),
            "gate_result": dict(fields.get("gate_result") or {}),
            "is_duplicate": bool(fields.get("is_duplicate", False)),
            "duplicate_of": fields.get("duplicate_of"),
            "parent_hypothesis_id": fields.get("parent_hypothesis_id"),
            "campaign_id": fields.get("campaign_id"),
            "strategy_family": fields.get("strategy_family"),
            "strategy_version": fields.get("strategy_version"),
            "feature_set_hash": fields.get("feature_set_hash"),
            "parameter_hash": fields.get("parameter_hash"),
            "validation_protocol_version": fields.get("validation_protocol_version"),
        }

        # Enforce exact reproducibility fingerprints on accepted experiments
        if self.fields["status"] == "accepted":
            missing = [
                k
                for k in (
                    "dataset_fingerprint",
                    "config_fingerprint",
                    "code_fingerprint",
                )
                if not self.fields.get(k)
            ]
            if missing:
                raise ResearchInputError(
                    "Accepted experiments require fingerprints for exact "
                    f"reproducibility: {missing}"
                )

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
    """Append-only JSONL ledger with a thread-safe hypothesis counter.

    Records are immutable once written: there is no update or delete path.
    The ledger stores successful, rejected, failed, and interrupted runs so
    the research history can never be rewritten into a winners-only list.
    """

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

    @staticmethod
    def _research_fingerprint(record: HypothesisRecord) -> str:
        """Deterministic duplicate key for one research record.

        Two records with the same key are the *same experiment* (same
        strategy, parameters, dataset, code, cost model, and period) — even
        if they carry different hypothesis ids. Records that lack
        fingerprints (e.g. minimal manual entries) are never treated as
        duplicates because they cannot prove identity.
        """
        if not (
            record.dataset_fingerprint
            and record.config_fingerprint
            and record.code_fingerprint
        ):
            return ""
        payload = {
            "strategy": record.strategy,
            "parameters": record.parameters,
            "dataset_fingerprint": record.dataset_fingerprint,
            "config_fingerprint": record.config_fingerprint,
            "code_fingerprint": record.code_fingerprint,
            "dataset_version": record.dataset_version,
            "cost_model": record.cost_model,
            "backtest_period": record.backtest_period,
        }
        import hashlib

        encoded = json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]

    def find_duplicates(self) -> dict[str, list[str]]:
        """Group records by research fingerprint and report duplicates.

        Returns a mapping ``fingerprint -> [hypothesis_id, ...]`` for every
        fingerprint observed more than once. No record is ever removed or
        rewritten: duplication is reported, never silently ignored.
        """
        with self._lock:
            groups: dict[str, list[str]] = {}
            for record in self._read_records():
                key = self._research_fingerprint(record)
                if not key:
                    continue
                groups.setdefault(key, []).append(record.hypothesis_id)
            return {key: ids for key, ids in groups.items() if len(ids) > 1}

    def verify_integrity(self) -> dict[str, Any]:
        """Validate that every ledger line is a well-formed record.

        Returns counts plus the first invalid line number when the ledger is
        corrupt (the file is never repaired automatically — an append-only
        ledger must be inspected, not silently rewritten).
        """
        with self._lock:
            if not self.path.exists():
                return {"valid": True, "records": 0, "invalid_line": None}
            records = 0
            for line_number, line in enumerate(
                self.path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                try:
                    HypothesisRecord.from_dict(json.loads(line))
                except (ValueError, TypeError, json.JSONDecodeError):
                    return {
                        "valid": False,
                        "records": records,
                        "invalid_line": line_number,
                    }
                records += 1
            return {"valid": True, "records": records, "invalid_line": None}

    def record(self, **fields: Any) -> HypothesisRecord:
        """Append one entry, allocating a hypothesis id when not supplied.

        Duplicate *hypothesis ids* are always rejected. Duplicate research
        fingerprints are detected and marked (``is_duplicate`` /
        ``duplicate_of``); pass ``reject_duplicates=True`` to refuse them.
        """
        with self._lock:
            existing = {rec.hypothesis_id: rec for rec in self._read_records()}
            reject_duplicates = bool(fields.pop("reject_duplicates", False))
            if "hypothesis_id" not in fields or not fields.get("hypothesis_id"):
                fields["hypothesis_id"] = self.next_hypothesis_id()
            elif fields["hypothesis_id"] in existing:
                raise ResearchInputError(
                    f"Duplicate hypothesis_id: {fields['hypothesis_id']}"
                )
            candidate = HypothesisRecord(**fields)
            fingerprint = self._research_fingerprint(candidate)
            duplicate_of = None
            for record in existing.values():
                if self._research_fingerprint(record) == fingerprint and fingerprint:
                    duplicate_of = record.hypothesis_id
                    break
            if duplicate_of and reject_duplicates:
                raise DuplicateExperimentError(
                    f"duplicate experiment of {duplicate_of} "
                    f"(fingerprint {fingerprint}) — refusing to record"
                )
            candidate = HypothesisRecord(
                **{
                    **candidate.to_dict(),
                    "is_duplicate": duplicate_of is not None,
                    "duplicate_of": duplicate_of,
                }
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(candidate.to_dict(), sort_keys=True, default=str) + "\n"
                )
            return candidate

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

    def record_failure(
        self,
        hypothesis: str,
        *,
        strategy: str,
        reason: str,
        **details: Any,
    ) -> HypothesisRecord:
        """Record a failed run — failures are part of the audit trail."""
        return self.record(
            status="failed",
            hypothesis=hypothesis,
            strategy=strategy,
            reason=reason or "research run failed",
            **details,
        )

    def record_interruption(
        self,
        hypothesis: str,
        *,
        strategy: str,
        reason: str | None = None,
        **details: Any,
    ) -> HypothesisRecord:
        """Record an interrupted run — restarts are auditable events."""
        return self.record(
            status="interrupted",
            hypothesis=hypothesis,
            strategy=strategy,
            reason=reason or "run interrupted",
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
        holdout_period: str | None = None,
        universe_version: str | None = None,
        cost_model: str | None = None,
        dataset_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        code_fingerprint: str | None = None,
        run_id: str | None = None,
        gate_result: Mapping[str, Any] | None = None,
        reject_duplicates: bool = False,
    ) -> HypothesisRecord:
        """Record an :class:`Experiment` outcome using its own hypothesis id.

        ``gate_result`` carries the research-gate decision mapping; when
        ``reject_duplicates`` is True an identical research fingerprint
        (same strategy, parameters, dataset, code, cost model, period)
        raises :class:`DuplicateExperimentError` instead of being recorded.
        """
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
            holdout_period=holdout_period,
            universe_version=universe_version,
            cost_model=cost_model,
            metrics=dict(metrics or {}),
            reason=reason,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            code_fingerprint=code_fingerprint,
            experiment_id=experiment.experiment_id,
            run_id=run_id,
            gate_result=dict(gate_result or {}),
            reject_duplicates=reject_duplicates,
        )
