"""Strict AI-facing hypothesis contract for the deterministic research engine.

An AI research agent proposes hypotheses; this module validates them
against a strict pydantic schema. A proposal that does not validate is
rejected before any code path can consume it. Hypotheses describe *what to
test* — strategy family, registered strategy id, features, transformations,
parameters — never executable code.

The schema is deliberately rigid (``extra="forbid"``): unknown fields are
errors, so an agent cannot smuggle instructions, code, or untyped payloads
through the contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, field_validator

from .contracts import ResearchInputError

__all__ = [
    "ResearchHypothesis",
    "HypothesisValidationError",
    "hypothesis_fingerprint",
    "normalize_hypothesis",
    "parameter_variant_signature",
    "parameter_variant_signature_from_components",
]

_HYPOTHESIS_ID_RE = re.compile(r"^HYP-\d{5}$")

#: Parameter keys whose values do not participate in the variant signature
#: (non-behavioural bookkeeping).
_VARIANT_INERT_KEYS = frozenset({"fundamentals_rows", "description"})

#: Public alias for novelty/duplicate logic that compares raw parameter maps.
_INERT_KEYS = _VARIANT_INERT_KEYS


class HypothesisValidationError(ResearchInputError):
    """Raised when an AI proposal fails the hypothesis contract."""


def _json_safe(value: Any, path: str) -> None:
    try:
        json.dumps(value, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise HypothesisValidationError(f"{path} must be JSON-serializable") from exc


class ResearchHypothesis(BaseModel):
    """Validated description of one research idea.

    Fields mirror the research lineage contract: every hypothesis can point
    at its parent, its related prior work, its expected failure modes, and
    the exact feature/transformation/parameter specification that the
    deterministic engine will execute.
    """

    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str | None = None
    parent_hypothesis_id: str | None = None

    strategy_family: str
    strategy_id: str | None = None
    objective: str

    economic_rationale: str
    expected_mechanism: str
    novelty_reason: str = ""

    features: list[str] = []
    transformations: list[str] = []
    parameters: dict[str, Any] = {}

    related_prior_hypotheses: list[str] = []
    expected_failure_modes: list[str] = []

    confidence: float = 0.5

    @field_validator("hypothesis_id", "parent_hypothesis_id")
    @classmethod
    def _valid_hypothesis_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _HYPOTHESIS_ID_RE.match(str(value).strip()):
            raise HypothesisValidationError(
                f"hypothesis ids must match HYP-\\d{{5}}, got {value!r}"
            )
        return str(value).strip()

    @field_validator(
        "strategy_family", "objective", "economic_rationale", "expected_mechanism"
    )
    @classmethod
    def _non_empty(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise HypothesisValidationError("field must be non-empty")
        return normalized

    @field_validator("strategy_id")
    @classmethod
    def _strategy_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if not normalized or any(not (c.isalnum() or c == "_") for c in normalized):
            raise HypothesisValidationError(
                f"strategy_id must be a registry id like 'momentum', got {value!r}"
            )
        return normalized

    @field_validator("features", "transformations")
    @classmethod
    def _string_lists(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value]
        if any(not item for item in cleaned):
            raise HypothesisValidationError("list items must be non-empty strings")
        if len(set(cleaned)) != len(cleaned):
            raise HypothesisValidationError("list items must be unique")
        return cleaned

    @field_validator("parameters")
    @classmethod
    def _parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            _json_safe(item, f"parameters[{key!r}]")
        return dict(value)

    @field_validator("confidence")
    @classmethod
    def _confidence(cls, value: float) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise HypothesisValidationError("confidence must be numeric")
        if not 0.0 <= float(value) <= 1.0:
            raise HypothesisValidationError("confidence must be within [0, 1]")
        return float(value)

    @field_validator("related_prior_hypotheses")
    @classmethod
    def _prior_ids(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value]
        for item in cleaned:
            if not _HYPOTHESIS_ID_RE.match(item):
                raise HypothesisValidationError(
                    f"related_prior_hypotheses must contain HYP-\\d{{5}} ids, "
                    f"got {item!r}"
                )
        return cleaned


def normalize_hypothesis(payload: Mapping[str, Any]) -> ResearchHypothesis:
    """Validate an untrusted proposal mapping into a hypothesis.

    Unknown fields raise :class:`HypothesisValidationError` (the schema is
    ``extra="forbid"``); nothing in the payload is executed.
    """
    try:
        return ResearchHypothesis(**dict(payload))
    except HypothesisValidationError:
        raise
    except Exception as exc:  # pydantic validation errors
        raise HypothesisValidationError(f"invalid hypothesis payload: {exc}") from exc


def _variant_payload(
    strategy_family: str,
    strategy_id: str | None,
    features: Sequence[str],
    transformations: Sequence[str],
    parameters: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonical JSON payload for one idea's identity and variant signature."""
    return {
        "strategy_family": str(strategy_family).strip().lower(),
        "strategy_id": str(strategy_id).strip().lower() if strategy_id else None,
        "features": [str(item).strip().lower() for item in features],
        "transformations": [str(item).strip().lower() for item in transformations],
        "parameters": {
            key: value
            for key, value in dict(parameters).items()
            if key not in _VARIANT_INERT_KEYS
        },
    }


def hypothesis_fingerprint(hypothesis: ResearchHypothesis) -> str:
    """Deterministic identity of one hypothesis.

    Two hypotheses with the same fingerprint are the *same idea* (same
    family, strategy, features, transformations, and parameters). The
    fingerprint is used by the novelty controller to reject exact
    duplicates.
    """
    payload = _variant_payload(
        hypothesis.strategy_family,
        hypothesis.strategy_id,
        hypothesis.features,
        hypothesis.transformations,
        hypothesis.parameters,
    )
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def parameter_variant_signature(
    hypothesis: ResearchHypothesis,
) -> str:
    """Signature of the parameter choice within a family.

    Same family + strategy + features + transformations but different
    parameter values produce different signatures; the campaign budget uses
    these signatures to cap parameter variants per family.
    """
    return parameter_variant_signature_from_components(
        hypothesis.strategy_family,
        hypothesis.strategy_id,
        hypothesis.features,
        hypothesis.transformations,
        hypothesis.parameters,
    )


def parameter_variant_signature_from_components(
    strategy_family: str,
    strategy_id: str | None,
    features: Sequence[str],
    transformations: Sequence[str],
    parameters: Mapping[str, Any],
) -> str:
    """Signature from raw components (used for recorded-history comparison)."""
    payload = _variant_payload(
        strategy_family, strategy_id, features, transformations, parameters
    )
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
