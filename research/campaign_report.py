"""Human-readable campaign summaries. Not a winners-only leaderboard."""

from __future__ import annotations

from typing import Any

from .campaign import ResearchCampaignStore
from .ledger import HypothesisLedger
from .registry import BENCHMARK_ZOO

__all__ = ["campaign_summary_report"]


def campaign_summary_report(
    store: ResearchCampaignStore,
    ledger: HypothesisLedger,
    campaign_id: str,
) -> dict[str, Any]:
    campaign = store.latest(campaign_id)
    if campaign is None:
        return {"status": "INSUFFICIENT_DATA", "reason": "unknown campaign"}
    records = [
        rec
        for rec in ledger.list_records()
        if rec.campaign_id == campaign_id
        or campaign_id in str(rec.fields.get("campaign_id"))
    ]
    by_status: dict[str, int] = {}
    by_family: dict[str, int] = {}
    reasons: list[str] = []
    for rec in records:
        by_status[rec.status] = by_status.get(rec.status, 0) + 1
        family = rec.strategy_family or rec.strategy or "unknown"
        by_family[str(family)] = by_family.get(str(family), 0) + 1
        if rec.reason:
            reasons.append(str(rec.reason))
    explored = set(by_family)
    unexplored = [name for name in BENCHMARK_ZOO if name not in explored]
    return {
        "campaign": campaign.to_dict(),
        "how_many_tested": campaign.trial_count,
        "how_many_failed": campaign.failed_trials + campaign.rejected_trials,
        "how_many_insufficient_data": campaign.insufficient_data_trials,
        "how_many_passed": campaign.accepted_candidates,
        "how_many_duplicates": campaign.duplicate_trials,
        "families_tested": sorted(by_family),
        "family_counts": by_family,
        "status_counts": by_status,
        "failure_reasons": reasons,
        "budget_consumed": {
            "trials": campaign.trial_count,
            "max_trials": campaign.max_trials,
            "remaining": campaign.remaining_trials,
            "exhausted": campaign.budget_exhausted,
        },
        "unexplored_families": unexplored,
        "ledger_rows_for_campaign": len(records),
    }
