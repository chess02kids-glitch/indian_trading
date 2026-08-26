"""Multiple-testing accounting: authoritative trial counts for the DSR.

The deflated Sharpe ratio corrects an observed Sharpe for the number of
independent trials behind it. That number must be **search history**, not a
heuristic. This module answers, from the research ledger and campaign
store:

* how many trials have actually been attempted?
* are rejected/failed/insufficient-data/duplicate trials counted?
* are parameter variants counted?
* are campaigns distinguishable?
* was the count fixed before the final holdout evaluation?

Accounting rules (documented assumptions):

* A *trial* is a hypothesis that consumed a campaign reservation —
  statuses accepted, rejected, failed, insufficient_data, duplicate,
  abandoned, interrupted, halted, running. Schema-invalid proposals
  (``invalid``) never reserve a trial and are not counted; they are
  rejected before they can consume search budget.
* Benchmarks and placebo portfolios are **comparators**, not searched
  hypotheses: they do not enter the trial count. (The legacy gate
  heuristic counted them; the campaign/ledger path replaces that
  heuristic with real search history.)
* The count is fixed at reservation time — before the experiment and its
  holdout evaluation exist. Nothing in this module can add or remove a
  trial after a holdout result has been observed.
"""

from __future__ import annotations

from typing import Any

from .campaign import CampaignStore, ResearchCampaign
from .contracts import ResearchInputError
from .ledger import HypothesisLedger

__all__ = [
    "TRIAL_STATUSES",
    "dsr_accounting_report",
    "trial_count_from_history",
]

#: Ledger statuses that consumed a research trial (search history).
TRIAL_STATUSES = frozenset(
    {
        "accepted",
        "rejected",
        "failed",
        "insufficient_data",
        "duplicate",
        "abandoned",
        "interrupted",
        "halted",
        "running",
    }
)

#: Ledger statuses that never consumed search budget.
_NON_TRIAL_STATUSES = frozenset({"invalid"})


def trial_count_from_history(
    ledger: HypothesisLedger,
    *,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Count trials in the ledger, optionally scoped to one campaign.

    Returns a report mapping with the authoritative count, the per-status
    breakdown, and the non-trial (schema-invalid) count for transparency.
    """
    if not isinstance(ledger, HypothesisLedger):
        raise ResearchInputError("ledger must be a HypothesisLedger")
    # Use the latest-record-per-id view: a running reservation marker is
    # superseded by its outcome record, so every trial counts exactly once.
    latest = ledger.latest_records()
    records = [
        record
        for record in latest
        if campaign_id is None or record.campaign_id == campaign_id
    ]
    breakdown: dict[str, int] = {}
    for record in records:
        status = record.status
        breakdown[status] = breakdown.get(status, 0) + 1
    trials = sum(
        count for status, count in breakdown.items() if status in TRIAL_STATUSES
    )
    invalid = sum(
        count for status, count in breakdown.items() if status in _NON_TRIAL_STATUSES
    )
    return {
        "source": "ledger",
        "campaign_id": campaign_id,
        "trial_count": trials,
        "invalid_count": invalid,
        "breakdown": dict(sorted(breakdown.items())),
        "rules": {
            "counts": sorted(TRIAL_STATUSES),
            "excludes": sorted(_NON_TRIAL_STATUSES),
            "benchmarks_counted": False,
            "placebos_counted": False,
        },
    }


def campaign_trial_count(
    campaign: ResearchCampaign,
    ledger: HypothesisLedger,
) -> dict[str, Any]:
    """Authoritative trial count for a campaign.

    The campaign reservation counter is authoritative because it is fixed
    *before* any experiment runs. The ledger cross-check is reported, and a
    mismatch is surfaced (never silently repaired): the reservation log and
    the outcome ledger must agree.
    """
    ledger_report = trial_count_from_history(ledger, campaign_id=campaign.campaign_id)
    ledger_count = ledger_report["trial_count"]
    reservation_count = campaign.trial_count
    return {
        "source": "campaign",
        "campaign_id": campaign.campaign_id,
        "trial_count": reservation_count,
        "ledger_cross_check": ledger_count,
        "consistent": reservation_count == ledger_count,
        "fixed_before_holdout": True,
        "status": campaign.status,
        "per_family": dict(campaign.trials_by_family),
        "parameter_variants": {
            family: len(variants)
            for family, variants in campaign.variants_by_family.items()
        },
        "rules": {
            "counts": sorted(TRIAL_STATUSES),
            "excludes": sorted(_NON_TRIAL_STATUSES),
            "benchmarks_counted": False,
            "placebos_counted": False,
            "fixed_before_holdout": True,
        },
    }


def dsr_accounting_report(
    ledger: HypothesisLedger,
    campaign_store: CampaignStore | None = None,
    *,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Full multiple-testing accounting report for gate and reporting use.

    Returns the authoritative trial count plus provenance:

    * with a ``campaign_id`` and store: the campaign reservation count
      with the ledger cross-check;
    * with only a ledger: the ledger history count (all campaigns or one);
    * the ``trials_for_gate`` value is what should be passed to
      ``ResearchGate.evaluate(..., trials=...)``.
    """
    if campaign_id is not None and campaign_store is not None:
        campaign = campaign_store.load(campaign_id)
        if campaign is None:
            raise ResearchInputError(f"unknown campaign: {campaign_id}")
        report = campaign_trial_count(campaign, ledger)
    else:
        report = trial_count_from_history(ledger, campaign_id=campaign_id)
    report["trials_for_gate"] = int(report["trial_count"])
    return report


def legacy_gate_heuristic(benchmark_count: int, placebo_count: int) -> dict[str, Any]:
    """Describe the legacy gate heuristic for comparison and reporting.

    The legacy default (``len(benchmarks) + len(placebos) + 1``) counts
    comparators as trials. It is retained only as a documented fallback for
    callers that carry no campaign/ledger context; it is not search
    history.
    """
    if benchmark_count < 0 or placebo_count < 0:
        raise ResearchInputError("benchmark/placebo counts cannot be negative")
    return {
        "source": "heuristic",
        "trial_count": benchmark_count + placebo_count + 1,
        "benchmarks_counted": True,
        "placebos_counted": True,
        "candidate_counted": True,
        "warning": (
            "heuristic counts comparators as trials; use campaign/ledger "
            "accounting for gate decisions"
        ),
    }


def account_for_gate(
    ledger: HypothesisLedger,
    campaign_store: CampaignStore | None,
    *,
    campaign_id: str | None,
    benchmark_count: int,
    placebo_count: int,
) -> dict[str, Any]:
    """Choose the best available trial accounting for one gate decision."""
    if campaign_id is not None and campaign_store is not None:
        report = dsr_accounting_report(ledger, campaign_store, campaign_id=campaign_id)
        return report
    if ledger is not None:
        report = trial_count_from_history(ledger, campaign_id=campaign_id)
        report["trials_for_gate"] = report["trial_count"]
        return report
    return legacy_gate_heuristic(benchmark_count, placebo_count)
