"""Campaign research summary report tests (§23)."""

from __future__ import annotations

from research.campaign import CampaignStatus, CampaignStore
from research.campaign_report import (
    build_campaign_report,
    render_campaign_report_markdown,
    write_campaign_report,
)
from research.ledger import HypothesisLedger


def fill_history(ledger: HypothesisLedger, campaign_id: str) -> None:
    ledger.record(
        hypothesis_id="HYP-00001",
        status="rejected",
        strategy="momentum",
        strategy_family="momentum",
        campaign_id=campaign_id,
        reason="benchmark competitiveness",
    )
    ledger.record(
        hypothesis_id="HYP-00002",
        status="rejected",
        strategy="trend",
        strategy_family="trend",
        campaign_id=campaign_id,
        reason="turnover control",
    )
    ledger.record(
        hypothesis_id="HYP-00003",
        status="failed",
        strategy="quality",
        strategy_family="quality",
        campaign_id=campaign_id,
        reason="insufficient data: quality requires fundamentals",
    )
    ledger.record(
        hypothesis_id="HYP-00004",
        status="insufficient_data",
        strategy="low_volatility",
        strategy_family="volatility",
        campaign_id=campaign_id,
        reason="zoo family could not run",
    )
    ledger.record(
        hypothesis_id="HYP-00005",
        status="accepted",
        strategy="cross_sectional_momentum",
        strategy_family="momentum",
        campaign_id=campaign_id,
        dataset_fingerprint="ds",
        config_fingerprint="cfg",
        code_fingerprint="code",
    )
    ledger.record(
        hypothesis_id="HYP-00006",
        status="duplicate",
        strategy="momentum",
        strategy_family="momentum",
        campaign_id=campaign_id,
        reason="identical research fingerprint to HYP-00001",
    )


class TestCampaignReport:
    def test_counts_all_outcomes(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_history(ledger, "CMP-00001")
        report = build_campaign_report(ledger, campaign_id="CMP-00001")
        assert report["hypotheses_recorded"] == 6
        assert report["passed"] == 1
        assert report["rejected"] == 2
        assert report["failed"] == 1
        assert report["insufficient_data"] == 1
        assert report["duplicates"] == 1
        assert report["status_counts"]["accepted"] == 1

    def test_family_and_reason_histograms(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_history(ledger, "CMP-00001")
        report = build_campaign_report(ledger, campaign_id="CMP-00001")
        # momentum family: HYP-00001 (momentum) + HYP-00005
        # (cross_sectional_momentum) + HYP-00006 (duplicate) = 3
        assert report["family_counts"]["momentum"] == 3
        assert report["family_counts"]["trend"] == 1
        reasons = report["failure_reasons"]
        assert reasons["benchmark competitiveness"] == 1
        assert reasons["turnover control"] == 1
        assert reasons["insufficient data"] == 2

    def test_budget_consumption(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("report", ["momentum"])
        campaigns.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        campaigns.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            campaign_id=campaign.campaign_id,
        )
        report = build_campaign_report(ledger, campaigns)
        assert report["research_budget"]["consumed"] == 1
        assert report["research_budget"]["max"] == 60
        assert (
            report["research_budget"]["campaigns"][0]["status"] == CampaignStatus.ACTIVE
        )

    def test_unexplored_families(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_history(ledger, "CMP-00001")
        report = build_campaign_report(ledger)
        assert "mean_reversion" in report["unexplored_families"]

    def test_markdown_and_files(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_history(ledger, "CMP-00001")
        report = build_campaign_report(ledger)
        markdown = render_campaign_report_markdown(report)
        assert "# Campaign research summary" in markdown
        assert "passed: 1" in markdown
        assert "insufficient data: 1" in markdown
        json_path, md_path = write_campaign_report(report, tmp_path / "out")
        assert json_path.exists()
        assert md_path.exists()
