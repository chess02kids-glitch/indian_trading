"""Bounded research campaigns: how much searching has already occurred.

A campaign is the unit of search accounting. Every trial belongs to exactly
one campaign so an AI (or a human) can answer: how many ideas were tested,
how many failed, and whether the research budget is exhausted.

Campaign records are append-only. Status transitions are recorded as new
events; previous events are never rewritten.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .contracts import ResearchInputError

__all__ = [
    "CAMPAIGN_STATUSES",
    "ResearchBudgetExhausted",
    "ResearchCampaign",
    "ResearchCampaignStore",
    "campaign_id",
]

CAMPAIGN_STATUSES = (
    "draft",
    "active",
    "budget_exhausted",
    "completed",
    "abandoned",
)


class ResearchBudgetExhausted(ResearchInputError):
    """Raised when a campaign refuses further trials."""


def campaign_id(number: int) -> str:
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ResearchInputError("campaign number must be a positive integer")
    return f"CMP-{number:05d}"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ResearchCampaign:
    """Immutable snapshot of one research campaign."""

    campaign_id: str
    objective: str
    created_at: datetime
    strategy_families: tuple[str, ...]
    trial_count: int = 0
    active_trials: int = 0
    completed_trials: int = 0
    rejected_trials: int = 0
    accepted_candidates: int = 0
    insufficient_data_trials: int = 0
    duplicate_trials: int = 0
    failed_trials: int = 0
    invalid_trials: int = 0
    abandoned_trials: int = 0
    max_trials: int = 20
    max_trials_per_family: int = 4
    max_parameter_variants: int = 3
    status: str = "draft"
    notes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.campaign_id.strip():
            raise ResearchInputError("campaign_id is required")
        if not self.objective.strip():
            raise ResearchInputError("campaign objective is required")
        if self.status not in CAMPAIGN_STATUSES:
            raise ResearchInputError(
                "campaign status must be one of: " + ", ".join(CAMPAIGN_STATUSES)
            )
        if self.max_trials < 1 or self.max_trials_per_family < 1:
            raise ResearchInputError("campaign budgets must be positive")
        object.__setattr__(self, "notes", dict(self.notes))

    @property
    def remaining_trials(self) -> int:
        return max(0, self.max_trials - self.trial_count)

    @property
    def budget_exhausted(self) -> bool:
        return self.trial_count >= self.max_trials or self.status == "budget_exhausted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id,
            "objective": self.objective,
            "created_at": self.created_at.isoformat(),
            "strategy_families": list(self.strategy_families),
            "trial_count": self.trial_count,
            "active_trials": self.active_trials,
            "completed_trials": self.completed_trials,
            "rejected_trials": self.rejected_trials,
            "accepted_candidates": self.accepted_candidates,
            "insufficient_data_trials": self.insufficient_data_trials,
            "duplicate_trials": self.duplicate_trials,
            "failed_trials": self.failed_trials,
            "invalid_trials": self.invalid_trials,
            "abandoned_trials": self.abandoned_trials,
            "max_trials": self.max_trials,
            "max_trials_per_family": self.max_trials_per_family,
            "max_parameter_variants": self.max_parameter_variants,
            "status": self.status,
            "remaining_trials": self.remaining_trials,
            "notes": dict(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ResearchCampaign:
        created = payload.get("created_at")
        created_at = (
            datetime.fromisoformat(created) if isinstance(created, str) else created
        )
        return cls(
            campaign_id=str(payload["campaign_id"]),
            objective=str(payload["objective"]),
            created_at=created_at or _now(),
            strategy_families=tuple(payload.get("strategy_families") or ()),
            trial_count=int(payload.get("trial_count") or 0),
            active_trials=int(payload.get("active_trials") or 0),
            completed_trials=int(payload.get("completed_trials") or 0),
            rejected_trials=int(payload.get("rejected_trials") or 0),
            accepted_candidates=int(payload.get("accepted_candidates") or 0),
            insufficient_data_trials=int(payload.get("insufficient_data_trials") or 0),
            duplicate_trials=int(payload.get("duplicate_trials") or 0),
            failed_trials=int(payload.get("failed_trials") or 0),
            invalid_trials=int(payload.get("invalid_trials") or 0),
            abandoned_trials=int(payload.get("abandoned_trials") or 0),
            max_trials=int(payload.get("max_trials") or 20),
            max_trials_per_family=int(payload.get("max_trials_per_family") or 4),
            max_parameter_variants=int(payload.get("max_parameter_variants") or 3),
            status=str(payload.get("status") or "draft"),
            notes=dict(payload.get("notes") or {}),
        )


class ResearchCampaignStore:
    """Append-only JSONL store of campaign snapshots.

    Each mutation writes a new snapshot line. ``latest(campaign_id)`` is the
    last snapshot for that id. History is never deleted.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()

    def _read(self) -> list[ResearchCampaign]:
        if not self.path.exists():
            return []
        campaigns: list[ResearchCampaign] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            campaigns.append(ResearchCampaign.from_dict(json.loads(line)))
        return campaigns

    def _append(self, campaign: ResearchCampaign) -> ResearchCampaign:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(campaign.to_dict(), sort_keys=True) + "\n")
        return campaign

    def next_campaign_id(self) -> str:
        with self._lock:
            highest = 0
            for campaign in self._read():
                if campaign.campaign_id.startswith("CMP-"):
                    try:
                        highest = max(highest, int(campaign.campaign_id.split("-")[1]))
                    except ValueError:
                        continue
            return campaign_id(highest + 1)

    def create(
        self,
        objective: str,
        *,
        strategy_families: tuple[str, ...] = (),
        max_trials: int = 20,
        max_trials_per_family: int = 4,
        max_parameter_variants: int = 3,
        campaign_id_value: str | None = None,
        notes: Mapping[str, Any] | None = None,
    ) -> ResearchCampaign:
        with self._lock:
            campaign = ResearchCampaign(
                campaign_id=campaign_id_value or self.next_campaign_id(),
                objective=objective,
                created_at=_now(),
                strategy_families=tuple(strategy_families),
                max_trials=max_trials,
                max_trials_per_family=max_trials_per_family,
                max_parameter_variants=max_parameter_variants,
                status="active",
                notes=dict(notes or {}),
            )
            return self._append(campaign)

    def latest(self, campaign_id_value: str) -> ResearchCampaign | None:
        with self._lock:
            found = None
            for campaign in self._read():
                if campaign.campaign_id == campaign_id_value:
                    found = campaign
            return found

    def list_campaigns(self) -> tuple[ResearchCampaign, ...]:
        """Return the latest snapshot of each campaign, in first-seen order."""
        with self._lock:
            latest: dict[str, ResearchCampaign] = {}
            order: list[str] = []
            for campaign in self._read():
                if campaign.campaign_id not in latest:
                    order.append(campaign.campaign_id)
                latest[campaign.campaign_id] = campaign
            return tuple(latest[key] for key in order)

    def authorize_trial(
        self,
        campaign_id_value: str,
        *,
        family: str,
        family_trial_count: int,
        parameter_variant_count: int,
    ) -> ResearchCampaign:
        """Reserve one trial or raise ``ResearchBudgetExhausted``.

        ``family_trial_count`` and ``parameter_variant_count`` are supplied by
        the caller from ledger history so the store does not re-interpret
        experiments. The reservation increments ``trial_count`` and
        ``active_trials``.
        """
        with self._lock:
            campaign = self.latest(campaign_id_value)
            if campaign is None:
                raise ResearchInputError(f"unknown campaign: {campaign_id_value}")
            if campaign.status in {"completed", "abandoned", "budget_exhausted"}:
                raise ResearchBudgetExhausted(
                    f"RESEARCH_BUDGET_EXHAUSTED: campaign {campaign_id_value} "
                    f"is {campaign.status}"
                )
            if campaign.trial_count >= campaign.max_trials:
                payload = campaign.to_dict()
                payload.pop("remaining_trials", None)
                exhausted = ResearchCampaign(
                    **{
                        **payload,
                        "status": "budget_exhausted",
                        "created_at": campaign.created_at,
                        "strategy_families": campaign.strategy_families,
                    }
                )
                self._append(exhausted)
                raise ResearchBudgetExhausted(
                    f"RESEARCH_BUDGET_EXHAUSTED: max_trials={campaign.max_trials}"
                )
            if family_trial_count >= campaign.max_trials_per_family:
                raise ResearchBudgetExhausted(
                    f"RESEARCH_BUDGET_EXHAUSTED: family {family!r} already has "
                    f"{family_trial_count} trials "
                    f"(max {campaign.max_trials_per_family})"
                )
            if parameter_variant_count >= campaign.max_parameter_variants:
                raise ResearchBudgetExhausted(
                    "RESEARCH_BUDGET_EXHAUSTED: parameter variants exhausted "
                    f"({parameter_variant_count} >= {campaign.max_parameter_variants})"
                )
            families = campaign.strategy_families
            if family and family not in families:
                families = families + (family,)
            updated = ResearchCampaign(
                campaign_id=campaign.campaign_id,
                objective=campaign.objective,
                created_at=campaign.created_at,
                strategy_families=families,
                trial_count=campaign.trial_count + 1,
                active_trials=campaign.active_trials + 1,
                completed_trials=campaign.completed_trials,
                rejected_trials=campaign.rejected_trials,
                accepted_candidates=campaign.accepted_candidates,
                insufficient_data_trials=campaign.insufficient_data_trials,
                duplicate_trials=campaign.duplicate_trials,
                failed_trials=campaign.failed_trials,
                invalid_trials=campaign.invalid_trials,
                abandoned_trials=campaign.abandoned_trials,
                max_trials=campaign.max_trials,
                max_trials_per_family=campaign.max_trials_per_family,
                max_parameter_variants=campaign.max_parameter_variants,
                status="active",
                notes=campaign.notes,
            )
            return self._append(updated)

    def resolve_trial(
        self,
        campaign_id_value: str,
        *,
        outcome: str,
    ) -> ResearchCampaign:
        """Mark one active trial complete with a first-class outcome."""
        allowed = {
            "accepted": "accepted_candidates",
            "rejected": "rejected_trials",
            "failed": "failed_trials",
            "invalid": "invalid_trials",
            "insufficient_data": "insufficient_data_trials",
            "duplicate": "duplicate_trials",
            "abandoned": "abandoned_trials",
        }
        if outcome not in allowed:
            raise ResearchInputError(f"unknown trial outcome: {outcome}")
        with self._lock:
            campaign = self.latest(campaign_id_value)
            if campaign is None:
                raise ResearchInputError(f"unknown campaign: {campaign_id_value}")
            counter = allowed[outcome]
            kwargs = campaign.to_dict()
            kwargs["created_at"] = campaign.created_at
            kwargs["strategy_families"] = campaign.strategy_families
            kwargs["active_trials"] = max(0, campaign.active_trials - 1)
            kwargs["completed_trials"] = campaign.completed_trials + 1
            kwargs[counter] = int(kwargs[counter]) + 1
            if kwargs["trial_count"] >= campaign.max_trials:
                kwargs["status"] = "budget_exhausted"
            return self._append(ResearchCampaign.from_dict(kwargs))

    def summary(self, campaign_id_value: str) -> dict[str, Any]:
        campaign = self.latest(campaign_id_value)
        if campaign is None:
            raise ResearchInputError(f"unknown campaign: {campaign_id_value}")
        payload = campaign.to_dict()
        payload["budget_exhausted"] = campaign.budget_exhausted
        payload["unexplored_families"] = []
        return payload
