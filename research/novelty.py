"""Deterministic novelty control for research hypotheses.

The novelty controller answers one question: *is this hypothesis meaningfully
different from what has already been tested?* It is a deterministic,
code-only check — no learned models, no embeddings.

Outcomes:

* ``REJECTED_DUPLICATE`` — the exact idea (family, features,
  transformations, parameters) was already tested.
* ``REJECTED_NEAR_DUPLICATE`` — the family already has the maximum number
  of parameter variants; this proposal is another mutation of an already
  searched region and must not silently consume more search budget.
* ``ACCEPTED`` — a genuinely new idea, or a permitted variant.

Near duplicates are never silently collapsed: they are either recorded as
explicit new trials (when the campaign budget permits — the caller decides)
or rejected with the reason preserved in the research history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .campaign import ResearchBudget
from .contracts import ResearchInputError
from .hypotheses import (
    ResearchHypothesis,
    hypothesis_fingerprint,
    parameter_variant_signature_from_components,
)

__all__ = [
    "NoveltyCheck",
    "NoveltyController",
    "NoveltyVerdict",
    "RESEARCH_HISTORY_FIELDS",
]

#: Ledger record fields the novelty controller reads from research history.
#: ``strategy_family``/``features``/``transformations`` are optional on old
#: records; the controller falls back to the strategy name as the family
#: when the explicit family is absent.
RESEARCH_HISTORY_FIELDS = (
    "hypothesis_id",
    "status",
    "strategy",
    "strategy_family",
    "parameters",
    "features",
    "transformations",
    "campaign_id",
)


class NoveltyVerdict(str):
    """Outcomes of a novelty check."""

    ACCEPTED = "ACCEPTED"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"
    REJECTED_NEAR_DUPLICATE = "REJECTED_NEAR_DUPLICATE"


@dataclass(frozen=True, slots=True)
class NoveltyCheck:
    """One novelty decision with the evidence that produced it."""

    verdict: str
    reason: str
    duplicate_of: str | None = None
    variant_count: int = 0
    fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "duplicate_of": self.duplicate_of,
            "variant_count": self.variant_count,
            "fingerprint": self.fingerprint,
        }


def _record_mapping(record: Any) -> Mapping[str, Any]:
    """Normalise a history record (ledger entry or dict) to a mapping."""
    if hasattr(record, "to_dict"):
        return record.to_dict()
    if isinstance(record, Mapping):
        return record
    raise ResearchInputError(
        "research history records must be mappings or ledger records"
    )


def _family_of(fields: Mapping[str, Any]) -> str:
    family = fields.get("strategy_family")
    if family:
        return str(family).strip().lower()
    return str(fields.get("strategy") or "").strip().lower()


class NoveltyController:
    """Deterministic near-duplicate detection against research history."""

    def __init__(self, budget: ResearchBudget | None = None) -> None:
        self.budget = budget or ResearchBudget()

    def check(
        self,
        hypothesis: ResearchHypothesis,
        history: Sequence[Any],
    ) -> NoveltyCheck:
        """Compare one hypothesis against prior records.

        ``history`` items may be ledger records (objects exposing
        ``to_dict()``) or plain mappings with the fields named in
        :data:`RESEARCH_HISTORY_FIELDS`.
        """
        if not isinstance(hypothesis, ResearchHypothesis):
            raise ResearchInputError("novelty check requires a ResearchHypothesis")
        fingerprint = hypothesis_fingerprint(hypothesis)
        variant_signature = parameter_variant_signature_from_components(
            hypothesis.strategy_family,
            hypothesis.strategy_id,
            hypothesis.features,
            hypothesis.transformations,
            hypothesis.parameters,
        )

        same_signature_count = 0
        same_idea_count = 0
        duplicate_of: str | None = None
        hypothesis_features = frozenset(str(item) for item in hypothesis.features)
        hypothesis_transforms = frozenset(
            str(item) for item in hypothesis.transformations
        )
        hypothesis_typed = bool(hypothesis_features or hypothesis_transforms)
        for record in history:
            fields = _record_mapping(record)
            if _family_of(fields) != hypothesis.strategy_family.strip().lower():
                continue
            record_features = frozenset(
                str(item) for item in (fields.get("features") or [])
            )
            record_transforms = frozenset(
                str(item) for item in (fields.get("transformations") or [])
            )
            record_typed = bool(record_features or record_transforms)
            region_mismatch = (
                record_features != hypothesis_features
                or record_transforms != hypothesis_transforms
            )
            recorded_params = dict(fields.get("parameters") or {})
            # The recorded signature must be computed from the record's own
            # components — using the hypothesis components would make every
            # prior record look parameter-identical to the proposal.
            recorded_signature = parameter_variant_signature_from_components(
                hypothesis.strategy_family,
                hypothesis.strategy_id,
                [str(item) for item in record_features],
                [str(item) for item in record_transforms],
                recorded_params,
            )
            if region_mismatch:
                if record_typed != hypothesis_typed:
                    # One side lacks the feature/transformation evidence to
                    # establish its research region. Identity cannot be
                    # proven, so the record neither duplicates nor counts as
                    # a variant — blocking research on missing evidence
                    # would be worse than allowing an unknown overlap.
                    continue
                # Both sides typed and the regions differ: a different idea
                # — not a duplicate, not a variant.
                continue
            if recorded_signature == variant_signature:
                same_idea_count += 1
                if fields.get("hypothesis_id") and duplicate_of is None:
                    duplicate_of = str(fields["hypothesis_id"])
            else:
                same_signature_count += 1

        variant_count = same_idea_count + same_signature_count
        if duplicate_of is not None:
            return NoveltyCheck(
                verdict=NoveltyVerdict.REJECTED_DUPLICATE,
                reason=(
                    f"identical research fingerprint to {duplicate_of}; "
                    "the same idea was already tested"
                ),
                duplicate_of=duplicate_of,
                variant_count=variant_count,
                fingerprint=fingerprint,
            )
        if same_signature_count + same_idea_count > 0 and variant_count >= (
            self.budget.max_parameter_variants
        ):
            return NoveltyCheck(
                verdict=NoveltyVerdict.REJECTED_NEAR_DUPLICATE,
                reason=(
                    f"family {hypothesis.strategy_family!r} already has "
                    f"{variant_count} parameter variants tested (limit "
                    f"{self.budget.max_parameter_variants}); this is a "
                    "near-duplicate parameter mutation"
                ),
                variant_count=variant_count,
                fingerprint=fingerprint,
            )
        return NoveltyCheck(
            verdict=NoveltyVerdict.ACCEPTED,
            reason=(
                f"new idea (family {hypothesis.strategy_family!r}, "
                f"{variant_count} prior variants)"
            ),
            variant_count=variant_count,
            fingerprint=fingerprint,
        )
