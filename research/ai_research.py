"""AI research interface: proposals in, validated hypotheses out.

The AI agent's only entry point is :meth:`AIResearchInterface.submit_proposal`.
The flow is strictly one-way:

    AI proposal
      -> pydantic schema (extra=forbid)
      -> strategy registry (registered ids + parameter bounds only)
      -> novelty control (duplicate / near-duplicate rejection)
      -> campaign budget (trial reservation before any evaluation)
      -> validated hypothesis + hypothesis id

The interface NEVER executes code, never runs backtests, never calls the
gate, and never touches execution, brokers, risk, or capital allocation.
A validated hypothesis is executed by the deterministic research engine
(:mod:`research.runner` / zoo), which is the only component allowed to
produce results.

``ResearchContextBuilder`` supplies the agent with the research history it
needs to avoid rediscovering tested ideas. By default the context contains
NO holdout or performance results — the agent cannot condition its next
proposal on the outcomes of previous experiments unless the operator
explicitly passes ``include_results=True``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .campaign import CampaignStore
from .contracts import Experiment, ResearchInputError
from .hypotheses import (
    HypothesisValidationError,
    ResearchHypothesis,
    normalize_hypothesis,
    parameter_variant_signature,
)
from .ledger import HypothesisLedger
from .novelty import NoveltyCheck, NoveltyController, NoveltyVerdict
from .registry import StrategyRegistry

__all__ = [
    "AIResearchInterface",
    "ProposalResult",
    "ResearchContextBuilder",
]


class ProposalVerdict(str):
    """Outcomes of a proposal submission."""

    ACCEPTED = "ACCEPTED"
    REJECTED_INVALID = "REJECTED_INVALID"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
    REJECTED_NEAR_DUPLICATE = "REJECTED_NEAR_DUPLICATE"
    RESEARCH_BUDGET_EXHAUSTED = "RESEARCH_BUDGET_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class ProposalResult:
    """Structured result of one proposal submission."""

    verdict: str
    reason: str
    hypothesis: ResearchHypothesis | None = None
    hypothesis_id: str | None = None
    campaign_id: str | None = None
    novelty: NoveltyCheck | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "hypothesis_id": self.hypothesis_id,
            "campaign_id": self.campaign_id,
            "novelty": self.novelty.to_dict() if self.novelty else None,
            "hypothesis": (self.hypothesis.model_dump() if self.hypothesis else None),
        }


class ResearchContextBuilder:
    """Build the structured research-history context for an AI agent.

    The context is deterministic and derived exclusively from the ledger,
    the campaign store, and the code-defined registries. Performance
    results are excluded unless ``include_results=True`` is passed
    explicitly.
    """

    def __init__(
        self,
        ledger: HypothesisLedger,
        campaigns: CampaignStore | None = None,
        registry: StrategyRegistry | None = None,
    ) -> None:
        if not isinstance(ledger, HypothesisLedger):
            raise ResearchInputError("ledger must be a HypothesisLedger")
        self.ledger = ledger
        self.campaigns = campaigns
        self.registry = registry or StrategyRegistry()

    def build_context(
        self,
        *,
        campaign_id: str | None = None,
        include_results: bool = False,
    ) -> dict[str, Any]:
        """Return the AI-facing research context.

        ``include_results`` defaults to False: the agent receives what has
        been tried and why it was rejected, never how well it performed.
        """
        records = self.ledger.list_records()
        campaigns: list[dict[str, Any]] = []
        if self.campaigns is not None:
            for campaign in self.campaigns.list_campaigns():
                if campaign_id is not None and campaign.campaign_id != campaign_id:
                    continue
                campaigns.append(campaign.status_report())

        history: list[dict[str, Any]] = []
        for record in records:
            if campaign_id is not None and record.campaign_id != campaign_id:
                continue
            entry: dict[str, Any] = {
                "hypothesis_id": record.hypothesis_id,
                "status": record.status,
                "strategy": record.strategy,
                "strategy_family": record.strategy_family or record.strategy,
                "parameters": dict(record.parameters),
                "features": list(record.features),
                "transformations": list(record.transformations),
                "campaign_id": record.campaign_id,
                "parent_hypothesis_id": record.parent_hypothesis_id,
                "reason": record.reason,
            }
            if include_results:
                entry["metrics"] = dict(record.metrics)
                entry["gate_result"] = dict(record.gate_result)
            history.append(entry)

        failed_families: dict[str, list[str]] = {}
        for entry in history:
            family = entry["strategy_family"]
            if entry["status"] in ("rejected", "failed", "insufficient_data"):
                failed_families.setdefault(family, []).append(
                    entry["reason"] or entry["status"]
                )

        return {
            "research_budget": (
                {
                    "consumed": sum(c["trial_count"] for c in campaigns),
                    "campaigns": campaigns,
                }
                if campaigns
                else {"consumed": 0, "campaigns": []}
            ),
            "research_history": history,
            "failed_families": {
                family: reasons[:5] for family, reasons in failed_families.items()
            },
            "families_tested": dict(self.ledger.strategy_counts()),
            "available_strategies": self.registry.context(),
            "results_included": include_results,
            "rule": (
                "proposals must reference registered strategy ids with "
                "parameters inside declared bounds; no executable content "
                "is accepted"
            ),
        }


class AIResearchInterface:
    """The only entry point for AI-generated research proposals.

    Submission is pure validation and reservation: no computation on
    market data happens here, and no execution-capable module is imported
    by this module.
    """

    def __init__(
        self,
        ledger: HypothesisLedger,
        campaigns: CampaignStore,
        *,
        registry: StrategyRegistry | None = None,
        novelty: NoveltyController | None = None,
    ) -> None:
        if not isinstance(ledger, HypothesisLedger):
            raise ResearchInputError("ledger must be a HypothesisLedger")
        if not isinstance(campaigns, CampaignStore):
            raise ResearchInputError("campaigns must be a CampaignStore")
        self.ledger = ledger
        self.campaigns = campaigns
        self.registry = registry or StrategyRegistry()
        self.novelty = novelty or NoveltyController()

    def _record_invalid(
        self, payload: Mapping[str, Any], campaign_id: str | None, reason: str
    ) -> None:
        self.ledger.record(
            status="invalid",
            hypothesis=str(payload.get("objective") or "invalid proposal"),
            strategy=str(
                payload.get("strategy_id")
                or payload.get("strategy_family")
                or "unknown"
            ),
            strategy_family=str(payload.get("strategy_family") or ""),
            parameters=dict(payload.get("parameters") or {}),
            campaign_id=campaign_id,
            reason=reason,
        )

    def submit_proposal(
        self,
        payload: Mapping[str, Any],
        *,
        campaign_id: str,
        strategy_id: str | None = None,
    ) -> ProposalResult:
        """Validate one AI proposal and reserve a campaign trial.

        Returns :class:`ProposalResult` with one of the proposal verdicts.
        A rejected proposal is recorded in the ledger with its reason —
        rejections are research history, never deleted.
        """
        # 1) strict schema validation
        try:
            hypothesis = normalize_hypothesis(payload)
        except HypothesisValidationError as exc:
            self._record_invalid(payload, campaign_id, str(exc))
            return ProposalResult(
                verdict=ProposalVerdict.REJECTED_INVALID,
                reason=f"schema rejection: {exc}",
                campaign_id=campaign_id,
            )
        if strategy_id is not None:
            hypothesis = ResearchHypothesis(
                **{**hypothesis.model_dump(), "strategy_id": strategy_id}
            )

        # 2) registry validation (registered id + parameter bounds). The
        # strategy id must resolve to exactly one registered strategy; a
        # bare family name resolves only when the family is unambiguous.
        if hypothesis.strategy_id is None:
            family = hypothesis.strategy_family.strip().lower()
            candidates = [
                entry.registry_id
                for entry in self.registry.registry.values()
                if entry.family == family
            ]
            if len(candidates) == 1:
                hypothesis = ResearchHypothesis(
                    **{**hypothesis.model_dump(), "strategy_id": candidates[0]}
                )
            else:
                self._record_invalid(
                    payload,
                    campaign_id,
                    f"strategy_family {family!r} is ambiguous without a "
                    "strategy_id; candidates: " + ", ".join(candidates or ["(none)"]),
                )
                return ProposalResult(
                    verdict=ProposalVerdict.REJECTED_INVALID,
                    reason=(
                        f"family {family!r} does not resolve to a unique "
                        "registered strategy"
                    ),
                    campaign_id=campaign_id,
                )
        try:
            self.registry.validate_proposal(
                hypothesis.strategy_id,
                hypothesis.parameters,
            )
        except ResearchInputError as exc:
            self._record_invalid(payload, campaign_id, str(exc))
            return ProposalResult(
                verdict=ProposalVerdict.REJECTED_INVALID,
                reason=f"registry rejection: {exc}",
                campaign_id=campaign_id,
            )

        # 3) novelty control against recorded history (latest record per
        # id, so a trial is never counted twice).
        history = [
            record
            for record in self.ledger.latest_records()
            if campaign_id is None or record.campaign_id == campaign_id
        ]
        check = self.novelty.check(hypothesis, history)
        if check.verdict == NoveltyVerdict.REJECTED_DUPLICATE:
            self.ledger.record(
                hypothesis_id=(
                    hypothesis.hypothesis_id or self.ledger.next_hypothesis_id()
                ),
                status="duplicate",
                hypothesis=hypothesis.objective,
                strategy=hypothesis.strategy_id or hypothesis.strategy_family,
                strategy_family=hypothesis.strategy_family,
                parameters=dict(hypothesis.parameters),
                features=list(hypothesis.features),
                transformations=list(hypothesis.transformations),
                campaign_id=campaign_id,
                parent_hypothesis_id=hypothesis.parent_hypothesis_id,
                reason=check.reason,
                duplicate_of=check.duplicate_of,
            )
            return ProposalResult(
                verdict=ProposalVerdict.REJECTED_DUPLICATE,
                reason=check.reason,
                hypothesis=hypothesis,
                hypothesis_id=hypothesis.hypothesis_id,
                campaign_id=campaign_id,
                novelty=check,
            )
        if check.verdict == NoveltyVerdict.REJECTED_NEAR_DUPLICATE:
            self.ledger.record(
                hypothesis_id=(
                    hypothesis.hypothesis_id or self.ledger.next_hypothesis_id()
                ),
                status="duplicate",
                hypothesis=hypothesis.objective,
                strategy=hypothesis.strategy_id or hypothesis.strategy_family,
                strategy_family=hypothesis.strategy_family,
                parameters=dict(hypothesis.parameters),
                features=list(hypothesis.features),
                transformations=list(hypothesis.transformations),
                campaign_id=campaign_id,
                parent_hypothesis_id=hypothesis.parent_hypothesis_id,
                reason=check.reason,
            )
            return ProposalResult(
                verdict=ProposalVerdict.REJECTED_NEAR_DUPLICATE,
                reason=check.reason,
                hypothesis=hypothesis,
                hypothesis_id=hypothesis.hypothesis_id,
                campaign_id=campaign_id,
                novelty=check,
            )

        # 4) campaign budget reservation (before any evaluation). The
        # budget is probed first (no resources are consumed for a trial
        # that could not run); then the hypothesis id is allocated
        # atomically by the ledger (a running reservation record), so
        # consecutive submissions never collide.
        allowed, budget_error = self.campaigns.can_reserve(
            campaign_id,
            family=hypothesis.strategy_family,
            variant_signature=parameter_variant_signature(hypothesis),
        )
        if not allowed:
            return ProposalResult(
                verdict=ProposalVerdict.RESEARCH_BUDGET_EXHAUSTED,
                reason=budget_error or "research budget exhausted",
                hypothesis=hypothesis,
                campaign_id=campaign_id,
                novelty=check,
            )
        reservation = self.ledger.reserve(
            hypothesis=hypothesis.objective,
            strategy=hypothesis.strategy_id or hypothesis.strategy_family,
            strategy_family=hypothesis.strategy_family,
            parameters=dict(hypothesis.parameters),
            features=list(hypothesis.features),
            transformations=list(hypothesis.transformations),
            campaign_id=campaign_id,
            parent_hypothesis_id=hypothesis.parent_hypothesis_id,
        )
        hypothesis_id = reservation.hypothesis_id
        self.campaigns.reserve_trial(
            campaign_id,
            hypothesis_id,
            family=hypothesis.strategy_family,
            variant_signature=parameter_variant_signature(hypothesis),
        )
        hypothesis = ResearchHypothesis(
            **{**hypothesis.model_dump(), "hypothesis_id": hypothesis_id}
        )
        return ProposalResult(
            verdict=ProposalVerdict.ACCEPTED,
            reason=check.reason,
            hypothesis=hypothesis,
            hypothesis_id=hypothesis_id,
            campaign_id=campaign_id,
            novelty=check,
        )


def hypothesis_to_experiment(
    hypothesis: ResearchHypothesis,
    *,
    universe: str,
    dataset_version: str | None = None,
    cost_model: str | None = None,
) -> Experiment:
    """Build the deterministic engine contract from a validated hypothesis."""
    if not isinstance(hypothesis, ResearchHypothesis):
        raise ResearchInputError("hypothesis must be a ResearchHypothesis")
    return Experiment(
        hypothesis_id=hypothesis.hypothesis_id or "HYP-UNASSIGNED",
        strategy=hypothesis.strategy_id or hypothesis.strategy_family,
        parameters=dict(hypothesis.parameters),
        factor_set=list(hypothesis.features),
        universe=universe,
        dataset_version=dataset_version,
        cost_model=cost_model,
    )
