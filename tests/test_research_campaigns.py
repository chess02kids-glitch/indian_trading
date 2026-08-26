"""Campaign, hypothesis, budget, and novelty-control tests (v0.8)."""

from __future__ import annotations

import json

import pytest

from research.campaign import (
    BUDGET_EXHAUSTED_MESSAGE,
    BudgetExhaustedError,
    CampaignStatus,
    CampaignStore,
    ResearchBudget,
    campaign_id,
    parse_campaign_number,
)
from research.contracts import ResearchInputError
from research.hypotheses import (
    HypothesisValidationError,
    hypothesis_fingerprint,
    normalize_hypothesis,
    parameter_variant_signature,
)
from research.ledger import HypothesisLedger
from research.novelty import NoveltyController, NoveltyVerdict


def make_hypothesis(**overrides):
    """Canonical test hypothesis factory (validates through the strict path)."""
    payload = {
        "strategy_family": "momentum",
        "strategy_id": "momentum",
        "objective": "test trailing momentum on a synthetic world",
        "economic_rationale": "autocorrelated drift in the world generator",
        "expected_mechanism": "positive momentum persists over 63 days",
        "novelty_reason": "first momentum test in this campaign",
        "features": ["close"],
        "transformations": ["momentum_63"],
        "parameters": {"lookback": 63},
        "related_prior_hypotheses": [],
        "expected_failure_modes": ["turnover drag", "mean reversion regime"],
        "confidence": 0.5,
    }
    payload.update(overrides)
    return normalize_hypothesis(payload)


class TestCampaignIds:
    def test_format(self) -> None:
        assert campaign_id(1) == "CMP-00001"
        assert parse_campaign_number("CMP-00042") == 42

    def test_rejects_invalid(self) -> None:
        for bad in (0, -1, True):
            with pytest.raises(ResearchInputError):
                campaign_id(bad)
        for bad_id in ("CMP-1", "CMP-", "XMP-00001"):
            with pytest.raises(ResearchInputError):
                parse_campaign_number(bad_id)


class TestCampaignLifecycle:
    def test_create_and_sequence(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        first = store.create_campaign("zoo baseline", ["momentum"])
        second = store.create_campaign("leakage audit", ["trend"])
        assert first.campaign_id == "CMP-00001"
        assert second.campaign_id == "CMP-00002"
        assert first.status == CampaignStatus.ACTIVE

    def test_trial_accounting(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign(
            "accounting", ["momentum"], budget=ResearchBudget(max_trials=10)
        )
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        store.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")
        store.reserve_trial(campaign.campaign_id, "HYP-00002", family="momentum")
        store.record_outcome(campaign.campaign_id, "HYP-00002", status="accepted")
        state = store.require(campaign.campaign_id)
        assert state.trial_count == 2
        assert state.completed_trials == 2
        assert state.rejected_trials == 1
        assert state.accepted_candidates == 1
        assert state.trials_by_family == {"momentum": 2}
        assert state.active_trials == 0

    def test_outcome_requires_reservation(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("strict", ["momentum"])
        with pytest.raises(ResearchInputError):
            store.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")

    def test_double_reservation_rejected(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("strict", ["momentum"])
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        with pytest.raises(ResearchInputError):
            store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")

    def test_insufficient_data_status(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("data", ["momentum"])
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        store.record_outcome(
            campaign.campaign_id, "HYP-00001", status="insufficient_data"
        )
        state = store.require(campaign.campaign_id)
        assert state.insufficient_data_trials == 1
        assert state.completed_trials == 1

    def test_status_transitions_and_report(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("status", ["momentum"])
        store.set_status(campaign.campaign_id, CampaignStatus.COMPLETED)
        report = store.status_report(campaign.campaign_id)
        assert report["status"] == CampaignStatus.COMPLETED
        assert report["remaining_trials"] == report["research_budget"]["max_trials"]


class TestResearchBudget:
    def test_budget_validation(self) -> None:
        with pytest.raises(ResearchInputError):
            ResearchBudget(max_trials=0)
        with pytest.raises(ResearchInputError):
            ResearchBudget(max_trials_per_family=0)
        with pytest.raises(ResearchInputError):
            ResearchBudget(max_parameter_variants=0)
        # variants cannot exceed per-family trials
        with pytest.raises(ResearchInputError):
            ResearchBudget(max_trials_per_family=2, max_parameter_variants=5)

    def test_max_trials_exhaustion(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign(
            "tiny", ["momentum"], budget=ResearchBudget(max_trials=1)
        )
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        store.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")
        with pytest.raises(BudgetExhaustedError) as excinfo:
            store.reserve_trial(campaign.campaign_id, "HYP-00002", family="momentum")
        assert excinfo.value.campaign_id == campaign.campaign_id
        assert BUDGET_EXHAUSTED_MESSAGE in str(excinfo.value)

    def test_per_family_exhaustion(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign(
            "family-capped",
            ["momentum", "quality"],
            budget=ResearchBudget(
                max_trials=10, max_trials_per_family=2, max_parameter_variants=2
            ),
        )
        for number in (1, 2):
            store.reserve_trial(
                campaign.campaign_id, f"HYP-{number:05d}", family="momentum"
            )
            store.record_outcome(
                campaign.campaign_id, f"HYP-{number:05d}", status="rejected"
            )
        # Another family is still allowed...
        store.reserve_trial(campaign.campaign_id, "HYP-00003", family="quality")
        # ...but momentum is exhausted.
        with pytest.raises(BudgetExhaustedError):
            store.reserve_trial(campaign.campaign_id, "HYP-00004", family="momentum")

    def test_parameter_variant_exhaustion(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign(
            "variant-capped",
            ["momentum"],
            budget=ResearchBudget(
                max_trials=10, max_trials_per_family=10, max_parameter_variants=2
            ),
        )
        variants = [
            make_hypothesis(parameters={"lookback": 63}),
            make_hypothesis(parameters={"lookback": 126}),
        ]
        for number, hypothesis in enumerate(variants, start=1):
            store.reserve_trial(
                campaign.campaign_id,
                f"HYP-{number:05d}",
                family="momentum",
                variant_signature=parameter_variant_signature(hypothesis),
            )
            store.record_outcome(
                campaign.campaign_id, f"HYP-{number:05d}", status="rejected"
            )
        third = make_hypothesis(parameters={"lookback": 90})
        with pytest.raises(BudgetExhaustedError):
            store.reserve_trial(
                campaign.campaign_id,
                "HYP-00003",
                family="momentum",
                variant_signature=parameter_variant_signature(third),
            )

    def test_budget_exhausted_status_marked(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign(
            "exhaust", ["momentum"], budget=ResearchBudget(max_trials=1)
        )
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        state = store.require(campaign.campaign_id)
        assert state.exhausted() is True
        store.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")
        state = store.require(campaign.campaign_id)
        assert state.exhausted() is True  # max_trials reached even after completion

    def test_replay_persistence(self, tmp_path) -> None:
        path = tmp_path / "campaigns.jsonl"
        store = CampaignStore(path)
        campaign = store.create_campaign("persist", ["momentum"])
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        store.record_outcome(campaign.campaign_id, "HYP-00001", status="failed")
        replayed = CampaignStore(path).load(campaign.campaign_id)
        assert replayed is not None
        assert replayed.trial_count == 1
        assert replayed.failed_trials == 1
        assert replayed.open_trials == []

    def test_verify_integrity(self, tmp_path) -> None:
        path = tmp_path / "campaigns.jsonl"
        store = CampaignStore(path)
        store.create_campaign("ok", ["momentum"])
        report = store.verify_integrity()
        assert report["valid"] is True
        assert len(report["campaigns"]) == 1
        path.write_text("not json\n", encoding="utf-8")
        report = store.verify_integrity()
        assert report["valid"] is False

    def test_unknown_campaign_rejected(self, tmp_path) -> None:
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        with pytest.raises(ResearchInputError):
            store.reserve_trial("CMP-99999", "HYP-00001", family="momentum")


class TestResearchHypothesisSchema:
    def test_valid_proposal(self) -> None:
        hypothesis = make_hypothesis()
        assert hypothesis.strategy_family == "momentum"
        assert hypothesis.confidence == 0.5

    def test_extra_fields_rejected(self) -> None:
        payload = make_hypothesis().model_dump()
        payload["execute_code"] = "import os; os.system('true')"
        with pytest.raises(HypothesisValidationError):
            normalize_hypothesis(payload)

    def test_confidence_bounds(self) -> None:
        for bad in (-0.1, 1.5):
            with pytest.raises(HypothesisValidationError):
                make_hypothesis(confidence=bad)

    def test_non_json_parameter_rejected(self) -> None:
        with pytest.raises(HypothesisValidationError):
            make_hypothesis(parameters={"window": object()})

    def test_bad_hypothesis_ids_rejected(self) -> None:
        with pytest.raises(HypothesisValidationError):
            make_hypothesis(hypothesis_id="HYP-1")
        with pytest.raises(HypothesisValidationError):
            make_hypothesis(parent_hypothesis_id="HYP-01")

    def test_empty_rationale_rejected(self) -> None:
        with pytest.raises(HypothesisValidationError):
            make_hypothesis(economic_rationale="")

    def test_fingerprints_stable_and_distinct(self) -> None:
        base = make_hypothesis()
        assert hypothesis_fingerprint(base) == hypothesis_fingerprint(make_hypothesis())
        assert hypothesis_fingerprint(
            make_hypothesis(parameters={"lookback": 126})
        ) != hypothesis_fingerprint(base)


class TestNoveltyController:
    def test_exact_duplicate_rejected(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            strategy_family="momentum",
            features=["close"],
            transformations=["momentum_63"],
            parameters={"lookback": 63},
            campaign_id="CMP-00001",
        )
        check = NoveltyController().check(make_hypothesis(), ledger.list_records())
        assert check.verdict == NoveltyVerdict.REJECTED_DUPLICATE
        assert check.duplicate_of == "HYP-00001"

    def test_parameter_variant_capped(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        for number, lookback in enumerate((63, 126, 90), start=1):
            ledger.record(
                hypothesis_id=f"HYP-{number:05d}",
                status="rejected",
                strategy="momentum",
                strategy_family="momentum",
                features=["close"],
                transformations=["momentum_63"],
                parameters={"lookback": lookback},
                campaign_id="CMP-00001",
            )
        controller = NoveltyController(ResearchBudget(max_parameter_variants=3))
        check = controller.check(
            make_hypothesis(parameters={"lookback": 40}), ledger.list_records()
        )
        assert check.verdict == NoveltyVerdict.REJECTED_NEAR_DUPLICATE
        assert check.variant_count == 3

    def test_different_feature_region_accepted(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            strategy_family="momentum",
            features=["close"],
            transformations=["momentum_63"],
            parameters={"lookback": 63},
            campaign_id="CMP-00001",
        )
        check = NoveltyController().check(
            make_hypothesis(features=["close", "high"], transformations=["ema_50"]),
            ledger.list_records(),
        )
        assert check.verdict == NoveltyVerdict.ACCEPTED

    def test_different_family_accepted(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            strategy_family="momentum",
            features=["close"],
            transformations=["momentum_63"],
            parameters={"lookback": 63},
            campaign_id="CMP-00001",
        )
        check = NoveltyController().check(
            make_hypothesis(strategy_family="quality", strategy_id="quality"),
            ledger.list_records(),
        )
        assert check.verdict == NoveltyVerdict.ACCEPTED

    def test_legacy_untyped_record_does_not_block_typed_idea(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            parameters={"lookback": 63},
        )
        check = NoveltyController().check(make_hypothesis(), ledger.list_records())
        assert check.verdict == NoveltyVerdict.ACCEPTED


class TestLedgerLineage:
    def test_parent_chain(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(hypothesis_id="HYP-00001", status="rejected", strategy="m")
        ledger.record(
            hypothesis_id="HYP-00002",
            status="rejected",
            strategy="m",
            parent_hypothesis_id="HYP-00001",
            campaign_id="CMP-00001",
        )
        ledger.record(
            hypothesis_id="HYP-00003",
            status="failed",
            strategy="m",
            parent_hypothesis_id="HYP-00002",
            campaign_id="CMP-00001",
        )
        chain = ledger.lineage("HYP-00003")
        assert [record.hypothesis_id for record in chain] == [
            "HYP-00001",
            "HYP-00002",
            "HYP-00003",
        ]

    def test_campaign_scoped_records(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="m",
            campaign_id="CMP-00001",
        )
        ledger.record(
            hypothesis_id="HYP-00002",
            status="accepted",
            strategy="m",
            campaign_id="CMP-00002",
            dataset_fingerprint="ds",
            config_fingerprint="cfg",
            code_fingerprint="code",
        )
        assert len(ledger.records_for_campaign("CMP-00001")) == 1
        assert ledger.records_for_campaign("CMP-00002")[0].status == "accepted"

    def test_status_counts_include_losers(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(hypothesis_id="HYP-00001", status="rejected", strategy="m")
        ledger.record(hypothesis_id="HYP-00002", status="failed", strategy="m")
        ledger.record(
            hypothesis_id="HYP-00003", status="insufficient_data", strategy="m"
        )
        ledger.record(
            hypothesis_id="HYP-00004",
            status="accepted",
            strategy="m",
            dataset_fingerprint="ds",
            config_fingerprint="cfg",
            code_fingerprint="code",
        )
        counts = ledger.status_counts()
        assert counts["rejected"] == 1
        assert counts["failed"] == 1
        assert counts["insufficient_data"] == 1
        assert counts["accepted"] == 1
        # every record is counted exactly once, across all statuses
        assert sum(counts.values()) == 4

    def test_old_records_remain_readable(self, tmp_path) -> None:
        path = tmp_path / "ledger.jsonl"
        path.write_text(
            json.dumps(
                {
                    "hypothesis_id": "HYP-00001",
                    "status": "rejected",
                    "hypothesis": "legacy",
                    "strategy": "momentum",
                    "parameters": {"lookback": 63},
                    "recorded_at": "2026-08-01T00:00:00+00:00",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        ledger = HypothesisLedger(path)
        records = ledger.list_records()
        assert len(records) == 1
        assert records[0].campaign_id is None
        assert records[0].parent_hypothesis_id is None
        assert records[0].strategy_family is None
