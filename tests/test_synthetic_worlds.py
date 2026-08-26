"""Controlled synthetic world tests: framework behavior when truth is known.

These tests verify the RESEARCH FRAMEWORK on data with known structure.
They are calibration, never evidence about real markets.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.benchmarks import benchmark_suite
from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.validation import run_walk_forward
from research.campaign import BudgetExhaustedError, CampaignStore, ResearchBudget
from research.contracts import ResearchInputError
from research.gate import ResearchGate, generate_placebo_results
from research.hypotheses import ResearchHypothesis, parameter_variant_signature
from research.ledger import HypothesisLedger
from research.registry import StrategyRegistry
from research.synthetic_worlds import (
    build_world,
    leak_feature_for,
    variant_factory,
)
from research.zoo import (
    ZOO_FAMILIES,
    IdentityConstructor,
    WeightPanelStrategy,
    run_benchmark_zoo,
)

ZOO_FAMILY_IDS = tuple(entry["family_id"] for entry in ZOO_FAMILIES)


def _engine() -> VectorBTResearchEngine:
    return VectorBTResearchEngine(
        config=BacktestConfig(
            rebalance_frequency="M",
            initial_cash=1_000_000.0,
            cost_model=IndiaCostModel(scenario="base"),
        )
    )


def _zoo_gate_summary(world, seed: int) -> dict[str, dict[str, object]]:
    """Run the zoo + gate for one world; returns per-family gate decisions."""
    engine = _engine()
    results = run_benchmark_zoo(
        world.market_data.close,
        fundamentals=world.fundamentals,
        membership=world.membership,
        engine=engine,
        seed=seed,
    )
    placebos = generate_placebo_results(
        world.market_data.close, engine=engine, samples=10, seed=seed
    )
    gate = ResearchGate(random_seed=seed, dataset_fingerprint=world.fingerprint())
    summary: dict[str, dict[str, object]] = {}
    for family_id in ZOO_FAMILY_IDS:
        result = results.get(family_id)
        if result is None:
            summary[family_id] = {"verdict": "INSUFFICIENT_DATA"}
            continue
        validation = run_walk_forward(
            WeightPanelStrategy(result.weights, name=family_id),
            world.market_data,
            IdentityConstructor(),
            engine,
            train_size=252,
            test_size=63,
            purge=20,
            embargo=5,
        )
        benchmarks = {
            other: results[other]
            for other in ZOO_FAMILY_IDS
            if other != family_id and other in results
        }
        decision = gate.evaluate(
            result,
            benchmarks=benchmarks,
            validation=validation,
            placebo_results=placebos,
            cost_model_name="india:base",
            validation_method="walk_forward",
            trials=len(ZOO_FAMILY_IDS),
            trials_source="campaign",
        )
        summary[family_id] = {
            "verdict": decision.verdict,
            "sharpe": float(result.metrics.sharpe),
            "dsr_probability": decision.metrics.get("deflated_sharpe_probability", 0.0),
            "checks": decision.checks,
            "metrics": decision.metrics,
        }
    return summary


class TestWorldDeterminism:
    @pytest.mark.parametrize("world_id", ["A", "B", "C", "D", "E", "F", "G"])
    def test_build_is_deterministic(self, world_id: str) -> None:
        first = build_world(world_id, seed=20260824)
        second = build_world(world_id, seed=20260824)
        assert first.fingerprint() == second.fingerprint()
        assert first.truth == second.truth

    def test_worlds_are_distinct(self) -> None:
        fingerprints = {
            world_id: build_world(world_id, seed=20260824).fingerprint()
            for world_id in "ABCDEFG"
        }
        assert len(set(fingerprints.values())) == 7

    def test_unknown_world_rejected(self) -> None:
        with pytest.raises(ResearchInputError):
            build_world("Z")


class TestWorldAPureNoise:
    def test_no_family_passes_gate(self) -> None:
        for seed in (20260824, 7):
            world = build_world("A", seed=seed, n_symbols=24, n_days=700)
            summary = _zoo_gate_summary(world, seed)
            passed = [
                family
                for family, outcome in summary.items()
                if outcome["verdict"] == "PASS"
            ]
            assert passed == [], f"families passed on noise world: {passed}"


class TestWorldBMomentum:
    def test_momentum_families_detect_structure(self) -> None:
        csm_sharpes = []
        for seed in (20260824, 7):
            world = build_world("B", seed=seed, n_symbols=24, n_days=700)
            summary = _zoo_gate_summary(world, seed)
            csm = summary["cross_sectional_momentum"]
            # The structure is detected: positive OOS Sharpe and the DSR
            # survives the multiple-testing correction.
            assert csm["sharpe"] > 0.5, f"seed {seed}: csm sharpe {csm['sharpe']}"
            assert csm["dsr_probability"] >= 0.95, f"seed {seed}: DSR weak"
            csm_sharpes.append(csm["sharpe"])
        assert np.mean(csm_sharpes) > 1.0

    def test_momentum_edge_is_stronger_than_on_noise(self) -> None:
        noise = _zoo_gate_summary(
            build_world("A", seed=42, n_symbols=24, n_days=700), 42
        )["cross_sectional_momentum"]
        momentum = _zoo_gate_summary(
            build_world("B", seed=42, n_symbols=24, n_days=700), 42
        )["cross_sectional_momentum"]
        assert momentum["sharpe"] > noise["sharpe"] + 1.0


class TestWorldCMeanReversion:
    def test_reversal_detects_structure(self) -> None:
        world = build_world("C", seed=20260824, n_symbols=24, n_days=700)
        summary = _zoo_gate_summary(world, 20260824)
        reversal = summary["mean_reversion"]
        assert reversal["sharpe"] > 0.5
        assert reversal["dsr_probability"] >= 0.95
        # ...but the canonical implementation churns: the gate's turnover
        # control must reject it. Detection without economic viability is
        # exactly what the gate is for.
        turnover_check = next(
            check for check in reversal["checks"] if check.name == "turnover_control"
        )
        assert turnover_check.status == "fail"
        assert reversal["verdict"] == "FAIL"


class TestWorldDRegime:
    def test_trend_beats_naive_passive(self) -> None:
        for seed in (42, 7, 20260824):
            world = build_world("D", seed=seed, n_symbols=24, n_days=700)
            summary = _zoo_gate_summary(world, seed)
            trend = summary["trend_following"]["sharpe"]
            equal = summary["equal_weight"]["sharpe"]
            assert trend > equal, (
                f"seed {seed}: trend {trend:.3f} <= equal weight {equal:.3f}"
            )


class TestWorldELeakage:
    def test_leak_feature_exists(self) -> None:
        world = build_world("E", seed=42, n_symbols=10, n_days=100)
        leak = leak_feature_for(world)
        assert leak.shape == world.market_data.close.shape
        # The leak is tomorrow's return: row t equals next-day return.
        close = world.market_data.close
        expected = close.shift(-1).div(close) - 1.0
        assert np.allclose(leak.iloc[:-1].to_numpy(), expected.iloc[:-1].to_numpy())

    def test_no_family_passes_on_leakage_world_without_leak(self) -> None:
        world = build_world("E", seed=20260824, n_symbols=24, n_days=700)
        summary = _zoo_gate_summary(world, 20260824)
        passed = [
            family
            for family, outcome in summary.items()
            if outcome["verdict"] == "PASS"
        ]
        assert passed == []


class TestWorldFSurvivorship:
    def test_pit_membership_removes_boost(self) -> None:
        world = build_world("F", seed=20260824, n_symbols=24, n_days=700)
        engine = _engine()
        # Naive: no membership — delisted losers stay eligible until the
        # end, so the portfolio holds them through their collapse (and the
        # backtest still sees their full price history).
        naive = run_benchmark_zoo(
            world.market_data.close,
            fundamentals=world.fundamentals,
            membership=None,
            engine=engine,
        )
        pit = run_benchmark_zoo(
            world.market_data.close,
            fundamentals=world.fundamentals,
            membership=world.membership,
            engine=engine,
        )
        # The survivorship structure injects negative drift into doomed
        # names; holding them (naive) must be worse than excluding them.
        assert (
            naive["cross_sectional_momentum"].metrics.sharpe
            <= pit["cross_sectional_momentum"].metrics.sharpe
        )

    def test_pit_never_selects_delisted(self) -> None:
        world = build_world("F", seed=20260824, n_symbols=24, n_days=700)
        engine = _engine()
        results = run_benchmark_zoo(
            world.market_data.close,
            fundamentals=world.fundamentals,
            membership=world.membership,
            engine=engine,
        )
        truth = world.truth
        delist_dates = {
            symbol: pd.Timestamp(date) for symbol, date in truth["delist_dates"].items()
        }

        # Selection happens at month-end targets; between rebalances the
        # engine legitimately holds pre-delist positions (the delist date
        # is not knowable at the prior month-end).
        def month_ends(frame: pd.DataFrame) -> pd.DataFrame:
            return frame[~frame.index.to_period("M").duplicated(keep="last")]

        for family_id in (
            "cross_sectional_momentum",
            "low_volatility",
            "mean_reversion",
        ):
            weights = month_ends(results[family_id].weights)
            for symbol, date in delist_dates.items():
                late = weights.loc[weights.index >= date, symbol]
                assert (late == 0.0).all(), (
                    f"{family_id} selected delisted {symbol} after {date}"
                )


class TestWorldGMultipleTesting:
    def test_budget_exhausts_and_lucky_variant_rejected(self, tmp_path) -> None:
        world = build_world("G", seed=42, n_symbols=16, n_days=400)
        store = CampaignStore(tmp_path / "campaigns.jsonl")
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaign = store.create_campaign(
            "world G test",
            ["momentum"],
            budget=ResearchBudget(
                max_trials=4, max_trials_per_family=4, max_parameter_variants=2
            ),
        )
        factory = variant_factory(world, seed=42)
        engine = _engine()
        registry = StrategyRegistry()
        trials_run = 0
        best_sharpe = -1e9
        try:
            for number in range(1, 20):
                hypothesis_id = f"HYP-{number:05d}"
                parameters = factory()
                hypothesis = ResearchHypothesis(
                    strategy_family="momentum",
                    strategy_id="cross_sectional_momentum",
                    objective="random variant",
                    economic_rationale="none",
                    expected_mechanism="random",
                    features=["close"],
                    transformations=["momentum_63"],
                    parameters=parameters,
                )
                store.reserve_trial(
                    campaign.campaign_id,
                    hypothesis_id,
                    family="momentum",
                    variant_signature=parameter_variant_signature(hypothesis),
                )
                strategy = registry.build("cross_sectional_momentum", parameters)
                weights = strategy.generate_signals(world.market_data)
                from portfolio.construction import EqualWeightConstructor

                weight_panel = EqualWeightConstructor().construct(
                    weights, world.market_data
                )
                result = engine.run(
                    world.market_data.close,
                    weight_panel,
                    strategy_name="random_variant",
                    universe_history=[],
                )
                sharpe = float(result.metrics.sharpe)
                best_sharpe = max(best_sharpe, sharpe)
                # DSR with the campaign's own search count.
                trials = store.require(campaign.campaign_id).trial_count
                gate = ResearchGate(random_seed=42)
                decision = gate.evaluate(
                    result,
                    benchmarks=benchmark_suite(
                        world.market_data.close,
                        weight_panel,
                        engine=engine,
                        random_seed=42,
                    ),
                    trials=trials,
                    trials_source="campaign",
                )
                status = "accepted" if decision.verdict == "PASS" else "rejected"
                store.record_outcome(campaign.campaign_id, hypothesis_id, status=status)
                ledger.record(
                    hypothesis_id=hypothesis_id,
                    status=status,
                    hypothesis="random variant",
                    strategy="cross_sectional_momentum",
                    strategy_family="momentum",
                    parameters=parameters,
                    campaign_id=campaign.campaign_id,
                    metrics={"sharpe": sharpe},
                )
                trials_run += 1
        except BudgetExhaustedError:
            pass
        # The search was bounded: far fewer than 19 trials ran, and the
        # budget is exhausted (either the trial cap or the variant cap).
        assert 1 <= trials_run <= 4
        state = store.require(campaign.campaign_id)
        assert state.exhausted() is True
        # No variant was ever promoted (pure noise + honest accounting).
        records = ledger.records_for_campaign(campaign.campaign_id)
        assert all(record.status != "accepted" for record in records)
        # Some lucky variant may look good; that is exactly why the gate
        # applies the campaign-level DSR correction.
        assert best_sharpe > -1.0  # sanity: the search did run real variants
