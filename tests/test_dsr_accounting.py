"""Multiple-testing (DSR) accounting tests: trial counts from real history."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.benchmarks import benchmark_suite
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from research.campaign import CampaignStore, ResearchBudget
from research.contracts import ResearchInputError
from research.dsr_accounting import (
    TRIAL_STATUSES,
    account_for_gate,
    campaign_trial_count,
    dsr_accounting_report,
    legacy_gate_heuristic,
    trial_count_from_history,
)
from research.gate import ResearchGate
from research.hypotheses import ResearchHypothesis, parameter_variant_signature
from research.ledger import HypothesisLedger
from research.zoo import run_zoo_family


def make_prices(n_symbols: int = 6, n_days: int = 400, seed: int = 11) -> pd.DataFrame:
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-02", periods=n_days)
    columns = [f"S{i}" for i in range(n_symbols)]
    returns = generator.normal(0.0005, 0.012, size=(n_days, n_symbols))
    prices = 100.0 * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(prices, index=index, columns=columns)


def fill_ledger(ledger: HypothesisLedger, campaign_id: str) -> None:
    """Three trial statuses in one campaign plus one schema-invalid record."""
    ledger.record(
        hypothesis_id="HYP-00001",
        status="rejected",
        strategy="momentum",
        campaign_id=campaign_id,
    )
    ledger.record(
        hypothesis_id="HYP-00002",
        status="failed",
        strategy="trend",
        campaign_id=campaign_id,
    )
    ledger.record(
        hypothesis_id="HYP-00003",
        status="insufficient_data",
        strategy="quality",
        campaign_id=campaign_id,
    )
    ledger.record(
        hypothesis_id="HYP-00004",
        status="invalid",
        strategy="momentum",
        campaign_id=campaign_id,
    )


class TestTrialCounting:
    def test_rejected_failed_insufficient_counted(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_ledger(ledger, "CMP-00001")
        report = trial_count_from_history(ledger)
        assert report["trial_count"] == 3
        assert report["invalid_count"] == 1
        assert report["breakdown"]["rejected"] == 1
        assert report["breakdown"]["failed"] == 1
        assert report["breakdown"]["insufficient_data"] == 1

    def test_duplicates_and_abandoned_counted(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="duplicate",
            strategy="momentum",
            campaign_id="CMP-00001",
        )
        ledger.record(
            hypothesis_id="HYP-00002",
            status="abandoned",
            strategy="trend",
            campaign_id="CMP-00001",
        )
        ledger.record(
            hypothesis_id="HYP-00003",
            status="running",
            strategy="quality",
            campaign_id="CMP-00001",
        )
        report = trial_count_from_history(ledger)
        assert report["trial_count"] == 3

    def test_campaign_scoping(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_ledger(ledger, "CMP-00001")
        ledger.record(
            hypothesis_id="HYP-00005",
            status="rejected",
            strategy="momentum",
            campaign_id="CMP-00002",
        )
        scoped = trial_count_from_history(ledger, campaign_id="CMP-00001")
        assert scoped["trial_count"] == 3
        assert trial_count_from_history(ledger)["trial_count"] == 4

    def test_all_trial_statuses_declared(self) -> None:
        assert {
            "accepted",
            "rejected",
            "failed",
            "insufficient_data",
            "duplicate",
            "abandoned",
            "interrupted",
            "halted",
            "running",
        } <= set(TRIAL_STATUSES)
        assert "invalid" not in TRIAL_STATUSES

    def test_benchmarks_never_counted(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        fill_ledger(ledger, "CMP-00001")
        report = trial_count_from_history(ledger)
        assert report["rules"]["benchmarks_counted"] is False
        assert report["rules"]["placebos_counted"] is False


class TestCampaignAccounting:
    def test_campaign_reservation_count_authoritative(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("dsr-audit", ["momentum", "trend"])
        # Reserve BEFORE any run — the count is fixed before holdout.
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        store.reserve_trial(campaign.campaign_id, "HYP-00002", family="trend")
        store.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            campaign_id=campaign.campaign_id,
        )
        ledger.record(
            hypothesis_id="HYP-00002",
            status="running",
            strategy="trend",
            campaign_id=campaign.campaign_id,
        )
        report = dsr_accounting_report(ledger, store, campaign_id=campaign.campaign_id)
        assert report["source"] == "campaign"
        assert report["trial_count"] == 2
        assert report["trials_for_gate"] == 2
        assert report["consistent"] is True
        assert report["fixed_before_holdout"] is True

    def test_reservation_and_ledger_mismatch_surfaced(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("mismatch", ["momentum"])
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        store.record_outcome(campaign.campaign_id, "HYP-00001", status="rejected")
        # Ledger entry missing -> cross-check reports the inconsistency.
        report = campaign_trial_count(store.require(campaign.campaign_id), ledger)
        assert report["trial_count"] == 1
        assert report["ledger_cross_check"] == 0
        assert report["consistent"] is False

    def test_parameter_variants_reported(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign(
            "variants", ["momentum"], budget=ResearchBudget(max_trials=20)
        )

        def h(lookback):
            return ResearchHypothesis(
                strategy_family="momentum",
                objective="o",
                economic_rationale="r",
                expected_mechanism="m",
                features=["close"],
                transformations=["momentum_63"],
                parameters={"lookback": lookback},
            )

        for number, lookback in enumerate((63, 126), start=1):
            hypothesis = h(lookback)
            store.reserve_trial(
                campaign.campaign_id,
                f"HYP-{number:05d}",
                family="momentum",
                variant_signature=parameter_variant_signature(hypothesis),
            )
            store.record_outcome(
                campaign.campaign_id, f"HYP-{number:05d}", status="rejected"
            )
        report = campaign_trial_count(store.require(campaign.campaign_id), ledger)
        assert report["parameter_variants"] == {"momentum": 2}

    def test_unknown_campaign_rejected(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        with pytest.raises(ResearchInputError):
            dsr_accounting_report(ledger, store, campaign_id="CMP-99999")


class TestGateIntegration:
    def test_explicit_trials_used_and_recorded(self) -> None:
        prices = make_prices()
        engine = VectorBTResearchEngine(config=BacktestConfig())
        result = run_zoo_family("low_volatility", prices, engine=engine)
        benchmarks = benchmark_suite(
            prices, result.weights, engine=engine, random_seed=42
        )
        gate = ResearchGate()
        decision = gate.evaluate(
            result,
            benchmarks=benchmarks,
            trials=12,
            trials_source="campaign",
        )
        assert decision.metrics["trials_corrected"] == 12
        assert decision.metrics["trials_source"] == "campaign"
        check = next(c for c in decision.checks if c.name == "statistical_confidence")
        assert check.evidence["trials"] == 12
        assert check.evidence["trials_source"] == "campaign"
        assert decision.reproducibility["trials_source"] == "campaign"

    def test_heuristic_fallback_recorded(self) -> None:
        prices = make_prices()
        engine = VectorBTResearchEngine(config=BacktestConfig())
        result = run_zoo_family("low_volatility", prices, engine=engine)
        benchmarks = benchmark_suite(
            prices, result.weights, engine=engine, random_seed=42
        )
        decision = ResearchGate().evaluate(result, benchmarks=benchmarks)
        expected = len(benchmarks) + 1
        assert decision.metrics["trials_corrected"] == expected
        assert decision.metrics["trials_source"] == "heuristic"

    def test_invalid_trials_value_rejected(self) -> None:
        prices = make_prices()
        engine = VectorBTResearchEngine(config=BacktestConfig())
        result = run_zoo_family("low_volatility", prices, engine=engine)
        benchmarks = benchmark_suite(
            prices, result.weights, engine=engine, random_seed=42
        )
        with pytest.raises(ResearchInputError):
            ResearchGate().evaluate(result, benchmarks=benchmarks, trials=0)
        with pytest.raises(ResearchInputError):
            ResearchGate().evaluate(
                result,
                benchmarks=benchmarks,
                trials=True,  # type: ignore[arg-type]
            )

    def test_account_for_gate_prefers_campaign(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = store.create_campaign("gate", ["momentum"])
        store.reserve_trial(campaign.campaign_id, "HYP-00001", family="momentum")
        ledger.record(
            hypothesis_id="HYP-00001",
            status="running",
            strategy="momentum",
            campaign_id=campaign.campaign_id,
        )
        report = account_for_gate(
            ledger,
            store,
            campaign_id=campaign.campaign_id,
            benchmark_count=5,
            placebo_count=50,
        )
        assert report["source"] == "campaign"
        assert report["trials_for_gate"] == 1
        # the heuristic would have said 56 — search history says 1
        assert report["trials_for_gate"] != 56

    def test_legacy_heuristic_documented(self) -> None:
        report = legacy_gate_heuristic(5, 50)
        assert report["trial_count"] == 56
        assert report["source"] == "heuristic"
        assert report["warning"]
