"""Strict research-hypothesis contract for AI and human proposals.

A hypothesis is a *proposal*, not executable code. The research engine
instantiates only registered strategy families with validated parameters.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import ResearchInputError

__all__ = [
    "REJECTED_DUPLICATE",
    "ResearchHypothesis",
    "hypothesis_fingerprint",
    "normalize_parameters",
    "novelty_check",
]


REJECTED_DUPLICATE = "REJECTED_DUPLICATE"


def normalize_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize parameters so equivalent ideas share a fingerprint."""
    normalized: dict[str, Any] = {}
    for key, value in sorted(parameters.items(), key=lambda item: str(item[0])):
        if isinstance(value, bool) or value is None:
            normalized[str(key)] = value
        elif isinstance(value, int):
            normalized[str(key)] = value
        elif isinstance(value, float):
            if value.is_integer():
                normalized[str(key)] = int(value)
            else:
                normalized[str(key)] = round(value, 10)
        elif isinstance(value, str):
            normalized[str(key)] = value.strip().lower()
        elif isinstance(value, Mapping):
            normalized[str(key)] = normalize_parameters(value)
        elif isinstance(value, (list, tuple)):
            normalized[str(key)] = [
                item.strip().lower() if isinstance(item, str) else item
                for item in value
            ]
        else:
            normalized[str(key)] = str(value)
    return normalized


def hypothesis_fingerprint(
    *,
    strategy_family: str,
    parameters: Mapping[str, Any],
    features: list[str],
    transformations: list[str],
    portfolio_construction: str = "inverse_volatility",
) -> str:
    payload = {
        "strategy_family": strategy_family.strip().lower(),
        "parameters": normalize_parameters(parameters),
        "features": sorted(item.strip().lower() for item in features),
        "transformations": sorted(item.strip().lower() for item in transformations),
        "portfolio_construction": portfolio_construction.strip().lower(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


class ResearchHypothesis(BaseModel):
    """Validated, non-executable research proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    hypothesis_id: str | None = None
    parent_hypothesis_id: str | None = None
    strategy_family: str
    objective: str
    economic_rationale: str
    expected_mechanism: str
    novelty_reason: str
    features: list[str] = Field(default_factory=list)
    transformations: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)
    related_prior_hypotheses: list[str] = Field(default_factory=list)
    expected_failure_modes: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    portfolio_construction: str = "inverse_volatility"
    campaign_id: str | None = None

    @field_validator("strategy_family", "objective", "economic_rationale")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("required text field is empty")
        return value.strip()

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, value: float) -> float:
        if not 0.0 <= float(value) <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        return float(value)

    @field_validator("features", "transformations", "expected_failure_modes")
    @classmethod
    def _clean_lists(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if str(item).strip()]

    def fingerprint(self) -> str:
        return hypothesis_fingerprint(
            strategy_family=self.strategy_family,
            parameters=self.parameters,
            features=self.features,
            transformations=self.transformations,
            portfolio_construction=self.portfolio_construction,
        )

    def lineage(self) -> dict[str, str | None]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "parent_hypothesis_id": self.parent_hypothesis_id,
            "strategy_family": self.strategy_family,
            "parameter_hash": hashlib.sha256(
                json.dumps(
                    normalize_parameters(self.parameters),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()[:16],
            "feature_set_hash": hashlib.sha256(
                json.dumps(
                    sorted(self.features), sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()[:16],
            "fingerprint": self.fingerprint(),
        }


def novelty_check(
    hypothesis: ResearchHypothesis,
    prior: list[ResearchHypothesis] | list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Deterministic near-duplicate check against prior hypotheses.

    Returns ``status='ok'`` or ``status=REJECTED_DUPLICATE``. Duplicates are
    never silently collapsed: the caller must record them explicitly.
    """
    candidate = hypothesis.fingerprint()
    matches: list[str] = []
    for item in prior:
        if isinstance(item, ResearchHypothesis):
            fingerprint = item.fingerprint()
            identifier = item.hypothesis_id or fingerprint
        else:
            fingerprint = hypothesis_fingerprint(
                strategy_family=str(item.get("strategy_family", "")),
                parameters=dict(item.get("parameters") or {}),
                features=list(item.get("features") or []),
                transformations=list(item.get("transformations") or []),
                portfolio_construction=str(
                    item.get("portfolio_construction") or "inverse_volatility"
                ),
            )
            identifier = str(item.get("hypothesis_id") or fingerprint)
        if fingerprint == candidate:
            matches.append(identifier)
    if matches:
        return {
            "status": REJECTED_DUPLICATE,
            "duplicate_of": matches[0],
            "matches": matches,
            "fingerprint": candidate,
        }
    return {"status": "ok", "fingerprint": candidate, "matches": []}


def require_registered_family(family: str, allowed: set[str]) -> str:
    normalized = family.strip().lower().replace("-", "_")
    if normalized not in allowed:
        raise ResearchInputError(
            f"strategy family {family!r} is not in the allowed registry"
        )
    return normalized
