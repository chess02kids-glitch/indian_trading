"""AI research interface. The AI never touches execution.

Allowed path:

    AI proposal
      → Pydantic ResearchHypothesis
      → registered strategy family
      → allowed parameter bounds
      → deterministic research engine
      → gate
      → ledger

Forbidden: exec(), dynamic imports from LLM text, broker/order/risk calls.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any

from .campaign import ResearchBudgetExhausted, ResearchCampaignStore
from .contracts import ResearchInputError
from .hypothesis import (
    REJECTED_DUPLICATE,
    ResearchHypothesis,
    novelty_check,
)
from .ledger import HypothesisLedger
from .registry import allowed_families, instantiate

__all__ = [
    "AI_FORBIDDEN_MODULES",
    "ALLOWED_PARAMETER_BOUNDS",
    "ResearchProposalError",
    "assert_no_executable_payload",
    "build_research_context",
    "submit_hypothesis",
]


AI_FORBIDDEN_MODULES = frozenset(
    {
        "broker",
        "execution",
        "risk_kill",
        "orchestration",
        "auth",
    }
)

ALLOWED_PARAMETER_BOUNDS: dict[str, tuple[float, float]] = {
    "lookback": (2, 252),
    "window": (2, 252),
    "fast_window": (2, 200),
    "slow_window": (5, 300),
    "quantile": (0.05, 1.0),
    "seed": (0, 2**31 - 1),
    "threshold": (-10.0, 10.0),
    "entry_zscore": (-5.0, -0.1),
}


class ResearchProposalError(ResearchInputError):
    """Raised when an AI/human proposal is structurally unsafe or invalid."""


def assert_no_executable_payload(payload: Mapping[str, Any]) -> None:
    """Refuse anything that looks like code rather than a schema."""
    forbidden_keys = {"code", "python", "exec", "eval", "source", "module"}
    extra = forbidden_keys & set(payload)
    if extra:
        raise ResearchProposalError(
            f"AI hypotheses may not include executable fields: {sorted(extra)}"
        )
    for value in payload.values():
        if isinstance(value, str) and _looks_like_python(value):
            raise ResearchProposalError(
                "AI hypotheses may not contain executable Python source"
            )


def _looks_like_python(text: str) -> bool:
    stripped = text.strip()
    if "import " in stripped or "def " in stripped or "class " in stripped:
        try:
            tree = ast.parse(stripped)
        except SyntaxError:
            return False
        return any(
            isinstance(
                node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)
            )
            for node in ast.walk(tree)
        )
    return False


def _check_parameter_bounds(parameters: Mapping[str, Any]) -> None:
    for key, value in parameters.items():
        if key not in ALLOWED_PARAMETER_BOUNDS:
            # Unknown keys are allowed only if they are not executable.
            if isinstance(value, (int, float, str, bool)) or value is None:
                continue
            raise ResearchProposalError(
                f"parameter {key!r} is not a scalar and is not allowed"
            )
        lo, hi = ALLOWED_PARAMETER_BOUNDS[key]
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ResearchProposalError(f"parameter {key!r} must be numeric") from exc
        if not lo <= numeric <= hi:
            raise ResearchProposalError(
                f"parameter {key}={value} outside allowed bounds [{lo}, {hi}]"
            )


def build_research_context(
    ledger: HypothesisLedger,
    campaign_store: ResearchCampaignStore | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Structured history an AI must see before proposing a new idea."""
    records = ledger.list_records()
    families: dict[str, dict[str, int]] = {}
    failure_reasons: list[str] = []
    fingerprints: list[str] = []
    for record in records:
        family = str(record.strategy or "unknown")
        bucket = families.setdefault(
            family, {"accepted": 0, "rejected": 0, "failed": 0, "other": 0}
        )
        status = record.status
        if status in bucket:
            bucket[status] += 1
        else:
            bucket["other"] += 1
        if status in {"rejected", "failed", "invalid"} and record.reason:
            failure_reasons.append(str(record.reason))
        if record.config_fingerprint:
            fingerprints.append(str(record.config_fingerprint))
    campaign = None
    if campaign_store is not None and campaign_id:
        campaign = campaign_store.summary(campaign_id)
    return {
        "previous_hypotheses": [record.to_dict() for record in records],
        "failed_families": sorted(
            name
            for name, counts in families.items()
            if counts["rejected"] + counts["failed"]
        ),
        "successful_families": sorted(
            name for name, counts in families.items() if counts["accepted"]
        ),
        "family_counts": families,
        "parameter_regions_tested": fingerprints,
        "failure_reasons": failure_reasons[:50],
        "research_budget_consumed": {
            "trials": len(records),
        },
        "current_campaign": campaign,
        "available_features": [
            "momentum_3m",
            "momentum_1m",
            "realized_volatility",
            "zscore_20d",
            "ma_crossover",
            "roe",
        ],
        "available_families": sorted(allowed_families()),
        "forbidden": sorted(AI_FORBIDDEN_MODULES),
    }


def submit_hypothesis(
    payload: Mapping[str, Any],
    *,
    ledger: HypothesisLedger,
    campaign_store: ResearchCampaignStore | None = None,
    prior: list[ResearchHypothesis] | None = None,
    family_trial_count: int = 0,
    parameter_variant_count: int = 0,
) -> dict[str, Any]:
    """Validate a proposal and, if novel, reserve a campaign trial.

    Does **not** run a backtest and never imports execution modules.
    """
    assert_no_executable_payload(payload)
    try:
        hypothesis = ResearchHypothesis.model_validate(payload)
    except Exception as exc:
        raise ResearchProposalError(f"hypothesis schema rejected: {exc}") from exc
    family = hypothesis.strategy_family.strip().lower().replace("-", "_")
    if family not in allowed_families():
        raise ResearchProposalError(
            f"strategy family {hypothesis.strategy_family!r} is not registered"
        )
    _check_parameter_bounds(hypothesis.parameters)
    # Instantiation proves the family is constructible without executing a
    # backtest. Quality may have empty fundamentals; that is INSUFFICIENT_DATA
    # later, not a schema error.
    instantiate(family, hypothesis.parameters)

    novelty = novelty_check(hypothesis, prior or [])
    if novelty["status"] == REJECTED_DUPLICATE:
        ledger.record(
            status="duplicate",
            hypothesis=hypothesis.objective,
            strategy=family,
            parameters=dict(hypothesis.parameters),
            reason=REJECTED_DUPLICATE,
            parent_hypothesis_id=hypothesis.parent_hypothesis_id,
            campaign_id=hypothesis.campaign_id,
        )
        return {
            "status": REJECTED_DUPLICATE,
            "novelty": novelty,
            "hypothesis": hypothesis.model_dump(),
        }

    if campaign_store is not None and hypothesis.campaign_id:
        try:
            campaign_store.authorize_trial(
                hypothesis.campaign_id,
                family=family,
                family_trial_count=family_trial_count,
                parameter_variant_count=parameter_variant_count,
            )
        except ResearchBudgetExhausted as exc:
            ledger.record(
                status="abandoned",
                hypothesis=hypothesis.objective,
                strategy=family,
                reason="RESEARCH_BUDGET_EXHAUSTED",
                campaign_id=hypothesis.campaign_id,
            )
            return {
                "status": "RESEARCH_BUDGET_EXHAUSTED",
                "detail": str(exc),
                "hypothesis": hypothesis.model_dump(),
            }

    return {
        "status": "accepted_for_research",
        "novelty": novelty,
        "hypothesis": hypothesis.model_dump(),
        "lineage": hypothesis.lineage(),
    }
