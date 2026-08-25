"""v0.8 research-integrity: campaigns, registry, PIT, AI boundary, worlds."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from research.ai_boundary import (
    ResearchProposalError,
    assert_no_executable_payload,
    build_research_context,
    submit_hypothesis,
)
from research.campaign import (
    ResearchBudgetExhausted,
    ResearchCampaignStore,
)
from research.campaign_report import campaign_summary_report
from research.corporate_actions import (
    CorporateAction,
    CorporateActionType,
    UnknownCorporateAction,
    apply_corporate_actions,
)
from research.hypothesis import (
    REJECTED_DUPLICATE,
    ResearchHypothesis,
    novelty_check,
)
from research.ledger import HypothesisLedger
from research.pit import rank_eligible
from research.registry import (
    BENCHMARK_ZOO,
    allowed_families,
    instantiate,
)
from research.worlds import (
    FRAMEWORK_VERIFICATION,
    available_worlds,
    build_world,
    future_information_present,
)


def test_benchmark_zoo_has_ten_families() -> None:
    assert len(BENCHMARK_ZOO) == 10
    assert allowed_families() == frozenset(BENCHMARK_ZOO)


def test_registry_instantiates_every_family() -> None:
    index = pd.date_range("2023-01-02", periods=260, freq="B")
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(np.full((260, 4), 0.001), axis=0)),
        index=index,
        columns=["A", "B", "C", "D"],
    )
    from research.contracts import MarketData

    data = MarketData(close=close)
    for family in BENCHMARK_ZOO:
        strategy = instantiate(family)
        signal = strategy.generate_signals(data)
        assert strategy.family == family or strategy.name
        assert "strategy_family" in signal.metadata
        assert signal.values.shape == close.shape


def test_unknown_family_rejected() -> None:
    with pytest.raises(Exception, match="not registered"):
        instantiate("llm_generated_alpha")


def test_rank_eligible_masks_before_ranking() -> None:
    values = pd.DataFrame(
        [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]],
        index=pd.date_range("2024-01-01", periods=2),
        columns=["A", "B", "C"],
    )
    membership = pd.DataFrame(
        [[True, True, False], [True, True, False]],
        index=values.index,
        columns=values.columns,
    )
    ranks = rank_eligible(values, membership)
    assert pd.isna(ranks.loc[values.index[0], "C"])
    # Only A and B compete; C must not occupy a percentile slot.
    assert ranks.loc[values.index[0], "B"] == pytest.approx(1.0)
    naive = values.rank(axis=1, pct=True, method="first")
    assert naive.loc[values.index[0], "C"] == pytest.approx(1.0)


def test_campaign_budget_exhaustion(tmp_path) -> None:
    store = ResearchCampaignStore(tmp_path / "campaigns.jsonl")
    campaign = store.create("zoo", max_trials=2, max_trials_per_family=2)
    store.authorize_trial(
        campaign.campaign_id,
        family="momentum",
        family_trial_count=0,
        parameter_variant_count=0,
    )
    store.authorize_trial(
        campaign.campaign_id,
        family="momentum",
        family_trial_count=1,
        parameter_variant_count=0,
    )
    with pytest.raises(ResearchBudgetExhausted, match="RESEARCH_BUDGET_EXHAUSTED"):
        store.authorize_trial(
            campaign.campaign_id,
            family="trend",
            family_trial_count=0,
            parameter_variant_count=0,
        )
    latest = store.latest(campaign.campaign_id)
    assert latest is not None
    assert latest.status == "budget_exhausted"


def test_campaign_per_family_cap(tmp_path) -> None:
    store = ResearchCampaignStore(tmp_path / "campaigns.jsonl")
    campaign = store.create("cap", max_trials=10, max_trials_per_family=1)
    store.authorize_trial(
        campaign.campaign_id,
        family="momentum",
        family_trial_count=0,
        parameter_variant_count=0,
    )
    with pytest.raises(ResearchBudgetExhausted, match="family"):
        store.authorize_trial(
            campaign.campaign_id,
            family="momentum",
            family_trial_count=1,
            parameter_variant_count=0,
        )


def test_ledger_new_statuses(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
    for status in (
        "invalid",
        "insufficient_data",
        "duplicate",
        "abandoned",
        "rejected",
        "failed",
    ):
        rec = ledger.record(status=status, hypothesis=status, strategy="x")
        assert rec.status == status
    assert len(ledger.list_records()) == 6


def test_lineage_parent_recorded(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
    parent = ledger.record(status="rejected", hypothesis="first", strategy="momentum")
    child = ledger.record(
        status="rejected",
        hypothesis="mutation",
        strategy="momentum",
        parent_hypothesis_id=parent.hypothesis_id,
        campaign_id="CMP-00001",
        strategy_family="momentum",
        parameter_hash="abc",
    )
    assert child.parent_hypothesis_id == "HYP-00001"
    assert child.campaign_id == "CMP-00001"


def test_novelty_rejects_duplicate() -> None:
    first = ResearchHypothesis(
        strategy_family="momentum",
        objective="3m momentum",
        economic_rationale="continuation",
        expected_mechanism="winners keep winning",
        novelty_reason="baseline",
        features=["momentum_3m"],
        parameters={"lookback": 63},
    )
    twin = ResearchHypothesis(
        strategy_family="momentum",
        objective="same idea restated",
        economic_rationale="continuation",
        expected_mechanism="winners keep winning",
        novelty_reason="none",
        features=["momentum_3m"],
        parameters={"lookback": 63.0},
    )
    result = novelty_check(twin, [first])
    assert result["status"] == REJECTED_DUPLICATE


def test_submit_hypothesis_rejects_code(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "l.jsonl")
    with pytest.raises(ResearchProposalError):
        submit_hypothesis(
            {
                "strategy_family": "momentum",
                "objective": "hack",
                "economic_rationale": "x",
                "expected_mechanism": "x",
                "novelty_reason": "x",
                "code": "print(1)",
            },
            ledger=ledger,
        )
    with pytest.raises(ResearchProposalError):
        assert_no_executable_payload({"objective": "import os\nos.system('rm')"})


def test_submit_hypothesis_happy_path(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "l.jsonl")
    store = ResearchCampaignStore(tmp_path / "c.jsonl")
    campaign = store.create("test")
    result = submit_hypothesis(
        {
            "strategy_family": "momentum",
            "objective": "canonical momentum",
            "economic_rationale": "short-term continuation in liquid names",
            "expected_mechanism": "underreaction",
            "novelty_reason": "first trial in campaign",
            "features": ["momentum_3m"],
            "parameters": {"lookback": 63},
            "campaign_id": campaign.campaign_id,
        },
        ledger=ledger,
        campaign_store=store,
    )
    assert result["status"] == "accepted_for_research"
    ctx = build_research_context(ledger, store, campaign.campaign_id)
    assert "momentum" in ctx["available_families"]
    assert "broker" in ctx["forbidden"]


def test_submit_duplicate_recorded(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "l.jsonl")
    hypo = {
        "strategy_family": "low_volatility",
        "objective": "low vol",
        "economic_rationale": "risk",
        "expected_mechanism": "leverage aversion",
        "novelty_reason": "first",
        "features": ["realized_volatility"],
        "parameters": {"window": 63},
    }
    first = ResearchHypothesis.model_validate(hypo)
    result = submit_hypothesis(hypo, ledger=ledger, prior=[first])
    assert result["status"] == REJECTED_DUPLICATE
    assert ledger.list_records()[0].status == "duplicate"


def test_unknown_corporate_action_not_silent() -> None:
    dates = pd.date_range("2023-01-01", periods=3)
    prices = pd.DataFrame({"AAA": [10.0, 10.0, 10.0]}, index=dates)
    action = CorporateAction(
        symbol="AAA",
        ex_date=date(2023, 1, 2),
        action_type=CorporateActionType.UNKNOWN,
    )
    with pytest.raises(UnknownCorporateAction, match="UNKNOWN_CORPORATE_ACTION"):
        apply_corporate_actions(prices, [action])
    merger = CorporateAction(
        symbol="AAA",
        ex_date=date(2023, 1, 2),
        action_type=CorporateActionType.MERGER,
    )
    with pytest.raises(UnknownCorporateAction):
        apply_corporate_actions(prices, [merger])


def test_synthetic_worlds_are_labelled() -> None:
    assert len(available_worlds()) == 7
    for name in available_worlds():
        world = build_world(name)
        assert world.metadata["kind"] == FRAMEWORK_VERIFICATION
        assert not world.data.close.empty


def test_world_b_momentum_ranks_winners() -> None:
    world = build_world("B")
    strategy = instantiate("momentum", {"lookback": 63, "quantile": 0.25})
    signal = strategy.generate_signals(world.data)
    last = signal.values.iloc[-1]
    winners = last[last > 0].index.tolist()
    # Persistent positive-drift names are S00..; they should dominate.
    assert any(name.startswith("S0") for name in winners)


def test_world_e_leakage_detected() -> None:
    world = build_world("E")
    leak = world.metadata["invalid_feature"]
    assert future_information_present(leak, world.data.close)


def test_world_f_survivorship_mask() -> None:
    world = build_world("F")
    last = world.membership.iloc[-1]
    assert bool(last.iloc[-1]) is False
    ranks = rank_eligible(world.data.close, world.membership)
    assert ranks.iloc[-1].isna().sum() >= 1


def test_world_g_many_randoms_do_not_auto_promote(tmp_path) -> None:
    """Many placebo variants must still be counted as trials, not winners."""
    world = build_world("G")
    ledger = HypothesisLedger(tmp_path / "l.jsonl")
    store = ResearchCampaignStore(tmp_path / "c.jsonl")
    campaign = store.create(
        "mt", max_trials=5, max_trials_per_family=5, max_parameter_variants=8
    )
    sharpes = []
    for seed in range(8):
        try:
            store.authorize_trial(
                campaign.campaign_id,
                family="random",
                family_trial_count=seed,
                parameter_variant_count=seed,
            )
        except ResearchBudgetExhausted:
            ledger.record(
                status="abandoned",
                hypothesis="random search",
                strategy="random",
                reason="RESEARCH_BUDGET_EXHAUSTED",
                campaign_id=campaign.campaign_id,
            )
            continue
        strategy = instantiate("random", {"seed": 1000 + seed, "quantile": 0.25})
        signal = strategy.generate_signals(world.data)
        rets = signal.values.mean(axis=1).pct_change().dropna()
        sharpes.append(float(rets.mean() / (rets.std() + 1e-12)))
        ledger.record(
            status="rejected",
            hypothesis=f"random-{seed}",
            strategy="random",
            campaign_id=campaign.campaign_id,
            reason="placebo family; not economic",
        )
        store.resolve_trial(campaign.campaign_id, outcome="rejected")
    latest = store.latest(campaign.campaign_id)
    assert latest is not None
    assert latest.accepted_candidates == 0
    assert (
        any(rec.status == "abandoned" for rec in ledger.list_records())
        or latest.budget_exhausted
    )
    report = campaign_summary_report(store, ledger, campaign.campaign_id)
    assert report["how_many_passed"] == 0
    assert report["budget_consumed"]["exhausted"] or report["how_many_tested"] >= 5


def test_ai_cannot_bypass_registry(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "l.jsonl")
    with pytest.raises(ResearchProposalError):
        submit_hypothesis(
            {
                "strategy_family": "exec_payload",
                "objective": "x",
                "economic_rationale": "x",
                "expected_mechanism": "x",
                "novelty_reason": "x",
            },
            ledger=ledger,
        )


def test_parameter_bounds(tmp_path) -> None:
    ledger = HypothesisLedger(tmp_path / "l.jsonl")
    with pytest.raises(ResearchProposalError, match="bounds"):
        submit_hypothesis(
            {
                "strategy_family": "momentum",
                "objective": "x",
                "economic_rationale": "x",
                "expected_mechanism": "x",
                "novelty_reason": "x",
                "parameters": {"lookback": 10_000},
            },
            ledger=ledger,
        )
