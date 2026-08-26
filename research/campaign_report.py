"""Campaign research summary report (§23).

Answers, from the ledger and campaign store:

* how many strategies were tested? how many failed? how many were
  insufficient-data? how many passed?
* which families were tested, and how often?
* why did failures occur (reason histogram)?
* how much research budget was consumed, and what remains?
* which families remain unexplored?

The report deliberately does NOT focus on the highest Sharpe: it is a
search-history summary, not a leaderboard.
"""

from __future__ import annotations

import json
from typing import Any

from .campaign import CampaignStore
from .contracts import ResearchInputError
from .ledger import HypothesisLedger

__all__ = ["build_campaign_report", "render_campaign_report_markdown"]


def _reason_category(reason: str | None) -> str:
    """Bucket a failure reason into a coarse category for the histogram."""
    text = (reason or "").lower()
    if "budget" in text:
        return "budget exhausted"
    if "duplicate" in text or "already tested" in text:
        return "duplicate / already tested"
    if "schema" in text or "invalid" in text or "registry" in text:
        return "invalid proposal"
    if "insufficient" in text or "could not run" in text:
        return "insufficient data"
    if "benchmark" in text:
        return "benchmark competitiveness"
    if "turnover" in text:
        return "turnover control"
    if "cost" in text:
        return "cost robustness"
    if "confidence" in text or "sharpe" in text or "dsr" in text:
        return "statistical confidence"
    if "validation" in text or "fold" in text:
        return "validation consistency"
    if "placebo" in text:
        return "placebo dominance"
    if "drawdown" in text:
        return "drawdown control"
    return "other"


def build_campaign_report(
    ledger: HypothesisLedger,
    campaigns: CampaignStore | None = None,
    *,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Build the full campaign summary report."""
    if not isinstance(ledger, HypothesisLedger):
        raise ResearchInputError("ledger must be a HypothesisLedger")
    records = (
        ledger.records_for_campaign(campaign_id)
        if campaign_id is not None
        else ledger.list_records()
    )
    # Use the latest record per hypothesis id so a running reservation
    # marker is superseded by its outcome.
    latest: dict[str, Any] = {}
    for record in records:
        latest[record.hypothesis_id] = record
    unique = list(latest.values())

    status_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for record in unique:
        status_counts[record.status] = status_counts.get(record.status, 0) + 1
        family = record.strategy_family or record.strategy or "unknown"
        family_counts[family] = family_counts.get(family, 0) + 1
        if record.status in ("rejected", "failed", "invalid", "insufficient_data"):
            category = _reason_category(record.reason)
            reason_counts[category] = reason_counts.get(category, 0) + 1

    budget: dict[str, Any] = {"campaigns": [], "consumed": 0, "max": 0}
    if campaigns is not None:
        selected = [
            campaign
            for campaign in campaigns.list_campaigns()
            if campaign_id is None or campaign.campaign_id == campaign_id
        ]
        for campaign in selected:
            report = campaign.status_report()
            budget["campaigns"].append(report)
            budget["consumed"] += report["trial_count"]
            budget["max"] += report["research_budget"]["max_trials"]

    tested_families = set(family_counts)
    registered_families: set[str] = set()
    try:
        from .registry import StrategyRegistry

        registered_families = set(StrategyRegistry().families())
    except Exception:  # registry is optional context
        pass
    unexplored = sorted(registered_families - tested_families)

    return {
        "report_type": "campaign_research_summary",
        "scope": "campaign" if campaign_id else "all_campaigns",
        "campaign_id": campaign_id,
        "hypotheses_recorded": len(unique),
        "status_counts": dict(sorted(status_counts.items())),
        "passed": status_counts.get("accepted", 0),
        "failed": status_counts.get("failed", 0),
        "rejected": status_counts.get("rejected", 0),
        "insufficient_data": status_counts.get("insufficient_data", 0),
        "duplicates": status_counts.get("duplicate", 0),
        "invalid": status_counts.get("invalid", 0),
        "family_counts": dict(sorted(family_counts.items())),
        "failure_reasons": dict(
            sorted(reason_counts.items(), key=lambda item: -item[1])
        ),
        "research_budget": budget,
        "unexplored_families": unexplored,
        "unresolved_trials": sum(
            1
            for record in unique
            if record.status in ("running", "interrupted", "halted")
        ),
    }


def render_campaign_report_markdown(report: dict[str, Any]) -> str:
    """Render the machine-readable report as human-readable Markdown."""
    lines = [
        "# Campaign research summary",
        "",
        f"- scope: `{report['scope']}`"
        + (f" `{report['campaign_id']}`" if report["campaign_id"] else ""),
        f"- hypotheses recorded: {report['hypotheses_recorded']}",
        f"- passed: {report['passed']}",
        f"- rejected: {report['rejected']}",
        f"- failed: {report['failed']}",
        f"- insufficient data: {report['insufficient_data']}",
        f"- duplicates rejected: {report['duplicates']}",
        f"- invalid proposals: {report['invalid']}",
        f"- unresolved (running/interrupted/halted): {report['unresolved_trials']}",
        "",
        "## Families tested",
        "",
        "| family | trials |",
        "| --- | ---: |",
    ]
    for family, count in report["family_counts"].items():
        lines.append(f"| {family} | {count} |")
    if report["unexplored_families"]:
        lines.extend(
            [
                "",
                "## Families registered but unexplored",
                "",
                ", ".join(report["unexplored_families"]),
            ]
        )
    lines.extend(
        ["", "## Why failures occurred", "", "| reason | count |", "| --- | ---: |"]
    )
    for reason, count in report["failure_reasons"].items():
        lines.append(f"| {reason} | {count} |")
    budget = report["research_budget"]
    lines.extend(
        [
            "",
            "## Research budget",
            "",
            f"- consumed: {budget['consumed']}",
            f"- configured maximum: {budget['max']}",
            f"- remaining: {max(0, budget['max'] - budget['consumed'])}",
            "",
        ]
    )
    return "\n".join(lines)


def write_campaign_report(
    report: dict[str, Any],
    output_dir: str,
) -> tuple[Any, Any]:
    """Write JSON and Markdown copies of the report."""
    from pathlib import Path

    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "campaign_summary.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    md_path = directory / "campaign_summary.md"
    md_path.write_text(render_campaign_report_markdown(report) + "\n", encoding="utf-8")
    return json_path, md_path
