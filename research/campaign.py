"""Research campaigns: bounded, auditable search over strategy families.

A campaign is the unit of *search* in this repository. It answers the
questions the multiple-testing controls depend on:

* how many trials have been attempted in total?
* how many per strategy family?
* how many parameter variants per family?
* how much of the research budget remains?

Every trial must be **reserved before it runs** (``reserve_trial``) and the
outcome recorded afterwards (``record_outcome``). The reservation happens
before any holdout result exists, so the trial count used by the deflated
Sharpe correction is fixed *before* the final evaluation — an agent cannot
retroactively hide or add trials after seeing the holdout.

The store is an append-only JSONL event log. Campaign state is replayed
from the events on load, so a campaign can never be silently rewritten.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import ResearchInputError

__all__ = [
    "BUDGET_EXHAUSTED_MESSAGE",
    "BudgetExhaustedError",
    "CampaignStore",
    "CampaignStatus",
    "ResearchBudget",
    "ResearchCampaign",
    "campaign_id",
    "parse_campaign_number",
]

_CAMPAIGN_ID_RE = re.compile(r"^CMP-(\d{5})$")

#: Outcomes that end a reserved trial (every status except running/planned).
OUTCOME_STATUSES = (
    "accepted",
    "rejected",
    "failed",
    "insufficient_data",
    "duplicate",
    "abandoned",
    "interrupted",
    "halted",
)

#: Prefix every budget-exhaustion error with this token so automation can
#: detect the condition without parsing human prose.
BUDGET_EXHAUSTED_MESSAGE = "RESEARCH_BUDGET_EXHAUSTED"


class BudgetExhaustedError(ResearchInputError):
    """Raised when a campaign trial would exceed its research budget."""

    def __init__(self, campaign_id: str, message: str) -> None:
        self.campaign_id = campaign_id
        super().__init__(f"{BUDGET_EXHAUSTED_MESSAGE} [{campaign_id}]: {message}")


class CampaignStatus(str):
    """Lifecycle states of a research campaign."""

    PLANNED = "planned"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ABANDONED = "abandoned"

    @classmethod
    def validate(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {
            cls.PLANNED,
            cls.ACTIVE,
            cls.PAUSED,
            cls.COMPLETED,
            cls.BUDGET_EXHAUSTED,
            cls.ABANDONED,
        }:
            raise ResearchInputError(f"unknown campaign status: {value!r}")
        return normalized


def campaign_id(number: int) -> str:
    """Format a 1-based campaign number as CMP-00001 style."""
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ResearchInputError("campaign number must be a positive integer")
    return f"CMP-{number:05d}"


def parse_campaign_number(value: str) -> int:
    match = _CAMPAIGN_ID_RE.match(value.strip())
    if not match:
        raise ResearchInputError(f"campaign id {value!r} is not in CMP-00001 form")
    return int(match.group(1))


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    """Conservative caps on how much search a campaign may perform.

    The default limits are deliberately small: the point of a budget is to
    make *exhaustion* reachable and reportable (``RESEARCH_BUDGET_EXHAUSTED``)
    instead of allowing unbounded strategy generation.
    """

    max_trials: int = 60
    max_trials_per_family: int = 12
    max_parameter_variants: int = 3

    def __post_init__(self) -> None:
        values = {
            "max_trials": self.max_trials,
            "max_trials_per_family": self.max_trials_per_family,
            "max_parameter_variants": self.max_parameter_variants,
        }
        for name, value in values.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ResearchInputError(f"{name} must be a positive integer")
        if self.max_parameter_variants > self.max_trials_per_family:
            raise ResearchInputError(
                "max_parameter_variants cannot exceed max_trials_per_family"
            )

    def to_dict(self) -> dict[str, int]:
        return {
            "max_trials": self.max_trials,
            "max_trials_per_family": self.max_trials_per_family,
            "max_parameter_variants": self.max_parameter_variants,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchBudget":
        return cls(
            max_trials=int(payload["max_trials"]),
            max_trials_per_family=int(payload["max_trials_per_family"]),
            max_parameter_variants=int(payload["max_parameter_variants"]),
        )


@dataclass(slots=True)
class ResearchCampaign:
    """Replayable snapshot of one campaign's search state."""

    campaign_id: str
    objective: str
    strategy_families: tuple[str, ...]
    budget: ResearchBudget
    status: str = CampaignStatus.PLANNED
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    trial_count: int = 0
    active_trials: int = 0
    completed_trials: int = 0
    rejected_trials: int = 0
    accepted_candidates: int = 0
    failed_trials: int = 0
    insufficient_data_trials: int = 0
    duplicate_trials: int = 0
    abandoned_trials: int = 0
    unresolved_trials: int = 0
    trials_by_family: dict[str, int] = field(default_factory=dict)
    #: family -> distinct parameter-variant signatures observed so far.
    variants_by_family: dict[str, list[str]] = field(default_factory=dict)
    #: hypothesis ids currently reserved but not yet completed.
    open_trials: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.campaign_id = str(self.campaign_id).strip()
        if not self.campaign_id:
            raise ResearchInputError("campaign_id must be non-empty")
        self.status = CampaignStatus.validate(self.status)
        self.objective = str(self.objective or "").strip()
        if not self.objective:
            raise ResearchInputError("campaign objective must be non-empty")
        self.strategy_families = tuple(
            str(family).strip() for family in self.strategy_families
        )
        if not self.strategy_families or any(
            not family for family in self.strategy_families
        ):
            raise ResearchInputError("strategy_families must be non-empty")

    # -- state queries --------------------------------------------------------

    def exhausted(self) -> bool:
        """Return True when any budget limit is reached."""
        if self.trial_count >= self.budget.max_trials:
            return True
        if any(
            count >= self.budget.max_trials_per_family
            for count in self.trials_by_family.values()
        ):
            return True
        return any(
            len(variants) >= self.budget.max_parameter_variants
            for variants in self.variants_by_family.values()
        )

    def remaining_trials(self) -> int:
        return max(0, self.budget.max_trials - self.trial_count)

    def status_report(self) -> dict[str, Any]:
        """Full machine-readable campaign state for reports and the AI context."""
        return {
            "campaign_id": self.campaign_id,
            "objective": self.objective,
            "status": self.status,
            "created_at": self.created_at,
            "strategy_families": list(self.strategy_families),
            "research_budget": self.budget.to_dict(),
            "trial_count": self.trial_count,
            "active_trials": self.active_trials,
            "completed_trials": self.completed_trials,
            "rejected_trials": self.rejected_trials,
            "accepted_candidates": self.accepted_candidates,
            "failed_trials": self.failed_trials,
            "insufficient_data_trials": self.insufficient_data_trials,
            "duplicate_trials": self.duplicate_trials,
            "abandoned_trials": self.abandoned_trials,
            "unresolved_trials": self.unresolved_trials,
            "trials_by_family": dict(self.trials_by_family),
            "variants_by_family": {
                family: len(variants)
                for family, variants in self.variants_by_family.items()
            },
            "budget_exhausted": self.exhausted(),
            "remaining_trials": self.remaining_trials(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "objective": self.objective,
            "strategy_families": list(self.strategy_families),
            "budget": self.budget.to_dict(),
            "status": self.status,
            "created_at": self.created_at,
            "trial_count": self.trial_count,
            "active_trials": self.active_trials,
            "completed_trials": self.completed_trials,
            "rejected_trials": self.rejected_trials,
            "accepted_candidates": self.accepted_candidates,
            "failed_trials": self.failed_trials,
            "insufficient_data_trials": self.insufficient_data_trials,
            "duplicate_trials": self.duplicate_trials,
            "abandoned_trials": self.abandoned_trials,
            "unresolved_trials": self.unresolved_trials,
            "trials_by_family": dict(self.trials_by_family),
            "variants_by_family": {
                family: list(variants)
                for family, variants in self.variants_by_family.items()
            },
            "open_trials": list(self.open_trials),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchCampaign":
        return cls(
            campaign_id=str(payload["campaign_id"]),
            objective=str(payload["objective"]),
            strategy_families=tuple(payload.get("strategy_families") or ()),
            budget=ResearchBudget.from_dict(payload["budget"]),
            status=str(payload.get("status", CampaignStatus.PLANNED)),
            created_at=str(payload.get("created_at") or datetime.now(UTC).isoformat()),
            trial_count=int(payload.get("trial_count", 0)),
            active_trials=int(payload.get("active_trials", 0)),
            completed_trials=int(payload.get("completed_trials", 0)),
            rejected_trials=int(payload.get("rejected_trials", 0)),
            accepted_candidates=int(payload.get("accepted_candidates", 0)),
            failed_trials=int(payload.get("failed_trials", 0)),
            insufficient_data_trials=int(payload.get("insufficient_data_trials", 0)),
            duplicate_trials=int(payload.get("duplicate_trials", 0)),
            abandoned_trials=int(payload.get("abandoned_trials", 0)),
            unresolved_trials=int(payload.get("unresolved_trials", 0)),
            trials_by_family=dict(payload.get("trials_by_family") or {}),
            variants_by_family={
                family: list(variants)
                for family, variants in (
                    payload.get("variants_by_family") or {}
                ).items()
            },
            open_trials=list(payload.get("open_trials") or ()),
        )


class CampaignStore:
    """Append-only JSONL event log of campaign lifecycle events.

    Each mutation appends one event line; the current state is replayed
    from the events. There is no update or delete path.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    # -- persistence ----------------------------------------------------------

    def _append(self, event: str, campaign_id: str, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "event": event,
            "campaign_id": campaign_id,
            "payload": dict(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, sort_keys=True, default=str) + "\n")

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ResearchInputError(
                    f"campaign store corrupt at line {line_number}"
                ) from exc
            if "event" not in parsed or "campaign_id" not in parsed:
                raise ResearchInputError(
                    f"campaign store invalid event at line {line_number}"
                )
            events.append(parsed)
        return events

    def _replay(self, campaign_id: str) -> ResearchCampaign | None:
        current: ResearchCampaign | None = None
        for event in self._read_events():
            if event["campaign_id"] != campaign_id:
                continue
            payload = event["payload"]
            if event["event"] == "created":
                current = ResearchCampaign.from_dict(payload)
            elif current is None:
                raise ResearchInputError(
                    f"campaign event {event['event']!r} before creation "
                    f"for {campaign_id}"
                )
            elif event["event"] == "trial_reserved":
                current = self._apply_reserve(current, payload)
            elif event["event"] == "trial_outcome":
                current = self._apply_outcome(current, payload)
            elif event["event"] == "status_changed":
                current.status = CampaignStatus.validate(payload["status"])
            else:
                raise ResearchInputError(f"unknown campaign event {event['event']!r}")
        return current

    @staticmethod
    def _apply_reserve(
        campaign: ResearchCampaign, payload: Mapping[str, Any]
    ) -> ResearchCampaign:
        family = str(payload["family"])
        hypothesis_id = str(payload["hypothesis_id"])
        variant_signature = payload.get("variant_signature")
        if hypothesis_id in campaign.open_trials:
            raise ResearchInputError(
                f"hypothesis {hypothesis_id} already reserved in {campaign.campaign_id}"
            )
        campaign.trial_count += 1
        campaign.active_trials += 1
        campaign.open_trials.append(hypothesis_id)
        campaign.trials_by_family[family] = campaign.trials_by_family.get(family, 0) + 1
        if variant_signature:
            variants = campaign.variants_by_family.setdefault(family, [])
            if variant_signature not in variants:
                variants.append(variant_signature)
        return campaign

    @staticmethod
    def _apply_outcome(
        campaign: ResearchCampaign, payload: Mapping[str, Any]
    ) -> ResearchCampaign:
        hypothesis_id = str(payload["hypothesis_id"])
        status = str(payload["status"])
        if status not in OUTCOME_STATUSES:
            raise ResearchInputError(
                f"{status!r} is not a campaign trial outcome status"
            )
        if hypothesis_id not in campaign.open_trials:
            raise ResearchInputError(
                f"outcome for unreserved hypothesis {hypothesis_id}"
            )
        campaign.open_trials = [
            item for item in campaign.open_trials if item != hypothesis_id
        ]
        campaign.active_trials = max(0, campaign.active_trials - 1)
        if status in ("interrupted", "halted"):
            campaign.unresolved_trials += 1
        else:
            campaign.completed_trials += 1
            if status == "accepted":
                campaign.accepted_candidates += 1
            elif status == "rejected":
                campaign.rejected_trials += 1
            elif status == "failed":
                campaign.failed_trials += 1
            elif status == "insufficient_data":
                campaign.insufficient_data_trials += 1
            elif status == "duplicate":
                campaign.duplicate_trials += 1
            elif status == "abandoned":
                campaign.abandoned_trials += 1
        return campaign

    # -- public API -----------------------------------------------------------

    def next_campaign_id(self) -> str:
        with self._lock:
            highest = 0
            for event in self._read_events():
                try:
                    highest = max(highest, parse_campaign_number(event["campaign_id"]))
                except ResearchInputError:
                    continue
            return campaign_id(highest + 1)

    def create_campaign(
        self,
        objective: str,
        strategy_families: Sequence[str],
        *,
        budget: ResearchBudget | None = None,
    ) -> ResearchCampaign:
        """Create a new campaign with the next free CMP-XXXXX id."""
        with self._lock:
            fresh = ResearchCampaign(
                campaign_id=self.next_campaign_id(),
                objective=objective,
                strategy_families=strategy_families,
                budget=budget or ResearchBudget(),
                status=CampaignStatus.ACTIVE,
            )
            self._append("created", fresh.campaign_id, fresh.to_dict())
            return fresh

    def load(self, campaign_id: str) -> ResearchCampaign | None:
        with self._lock:
            return self._replay(campaign_id)

    def list_campaigns(self) -> tuple[ResearchCampaign, ...]:
        with self._lock:
            ids = {event["campaign_id"] for event in self._read_events()}
            return tuple(
                campaign
                for campaign_id in sorted(ids)
                if (campaign := self._replay(campaign_id)) is not None
            )

    def require(self, campaign_id: str) -> ResearchCampaign:
        campaign = self.load(campaign_id)
        if campaign is None:
            raise ResearchInputError(f"unknown campaign: {campaign_id}")
        return campaign

    def _guard_budget(
        self, campaign: ResearchCampaign, family: str, variant_signature: str | None
    ) -> None:
        if campaign.status not in (CampaignStatus.ACTIVE, CampaignStatus.PLANNED):
            raise BudgetExhaustedError(
                campaign.campaign_id,
                f"campaign status is {campaign.status}; trials cannot be reserved",
            )
        if campaign.trial_count >= campaign.budget.max_trials:
            raise BudgetExhaustedError(
                campaign.campaign_id,
                f"max_trials={campaign.budget.max_trials} reached "
                f"(trial_count={campaign.trial_count})",
            )
        family_count = campaign.trials_by_family.get(family, 0)
        if family_count >= campaign.budget.max_trials_per_family:
            raise BudgetExhaustedError(
                campaign.campaign_id,
                f"max_trials_per_family={campaign.budget.max_trials_per_family} "
                f"reached for family {family!r}",
            )
        if variant_signature is not None:
            variants = campaign.variants_by_family.get(family, [])
            if (
                variant_signature not in variants
                and len(variants) >= campaign.budget.max_parameter_variants
            ):
                raise BudgetExhaustedError(
                    campaign.campaign_id,
                    f"max_parameter_variants={campaign.budget.max_parameter_variants} "
                    f"reached for family {family!r}",
                )

    def reserve_trial(
        self,
        campaign_id: str,
        hypothesis_id: str,
        *,
        family: str,
        variant_signature: str | None = None,
    ) -> ResearchCampaign:
        """Reserve one trial slot *before* the experiment runs.

        Raises :class:`BudgetExhaustedError` (message prefixed
        ``RESEARCH_BUDGET_EXHAUSTED``) when any budget cap would be exceeded.
        """
        with self._lock:
            campaign = self.require(campaign_id)
            self._guard_budget(campaign, family, variant_signature)
            self._append(
                "trial_reserved",
                campaign_id,
                {
                    "hypothesis_id": hypothesis_id,
                    "family": family,
                    "variant_signature": variant_signature,
                },
            )
            return self.require(campaign_id)

    def record_outcome(
        self,
        campaign_id: str,
        hypothesis_id: str,
        *,
        status: str,
    ) -> ResearchCampaign:
        """Record a trial outcome; ``status`` must be a campaign outcome."""
        with self._lock:
            self.require(campaign_id)
            self._append(
                "trial_outcome",
                campaign_id,
                {"hypothesis_id": hypothesis_id, "status": status},
            )
            return self.require(campaign_id)

    def set_status(
        self, campaign_id: str, status: str, *, reason: str | None = None
    ) -> ResearchCampaign:
        with self._lock:
            validated = CampaignStatus.validate(status)
            self.require(campaign_id)
            self._append(
                "status_changed",
                campaign_id,
                {"status": validated, "reason": reason},
            )
            return self.require(campaign_id)

    def status_report(self, campaign_id: str) -> dict[str, Any]:
        return self.require(campaign_id).status_report()

    def verify_integrity(self) -> dict[str, Any]:
        """Validate the event log and return per-campaign summaries."""
        with self._lock:
            try:
                campaigns = self.list_campaigns()
            except ResearchInputError as exc:
                return {"valid": False, "error": str(exc)}
            return {
                "valid": True,
                "campaigns": [c.status_report() for c in campaigns],
            }
