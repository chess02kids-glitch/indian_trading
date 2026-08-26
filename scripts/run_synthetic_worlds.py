"""Run the controlled synthetic worlds (A–G) through the full research stack.

For every world:

1. create a research campaign with a bounded budget;
2. reserve one trial per zoo family BEFORE any evaluation;
3. run the ten-family benchmark zoo on the world's data;
4. gate every family against the other families (DSR trials = campaign
   search count, fixed before evaluation);
5. record every outcome in the campaign store and the shared ledger;
6. write machine-readable JSON and Markdown summaries.

World G additionally runs a budget-bounded random-variant search to
demonstrate RESEARCH_BUDGET_EXHAUSTED end-to-end.

All results are framework verification on synthetic data — never evidence
about real Indian equities. Outputs are git-ignored under
``reports/generated/synthetic_worlds``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.costs import IndiaCostModel  # noqa: E402
from backtest.engine import BacktestConfig, VectorBTResearchEngine  # noqa: E402
from backtest.validation import run_walk_forward  # noqa: E402
from research.campaign import (  # noqa: E402
    BudgetExhaustedError,
    CampaignStatus,
    CampaignStore,
    ResearchBudget,
)
from research.dsr_accounting import dsr_accounting_report  # noqa: E402
from research.gate import ResearchGate, generate_placebo_results  # noqa: E402
from research.hypotheses import (  # noqa: E402
    ResearchHypothesis,
    parameter_variant_signature,
)
from research.ledger import HypothesisLedger  # noqa: E402
from research.registry import StrategyRegistry  # noqa: E402
from research.synthetic_worlds import WORLDS, build_world, variant_factory  # noqa: E402
from research.zoo import (  # noqa: E402
    ZOO_FAMILIES,
    IdentityConstructor,
    WeightPanelStrategy,
    run_benchmark_zoo,
)

OUTPUT_ROOT = ROOT / "reports" / "generated" / "synthetic_worlds"
LEDGER_PATH = OUTPUT_ROOT / "experiments" / "ledger.jsonl"
CAMPAIGNS_PATH = OUTPUT_ROOT / "experiments" / "campaigns.jsonl"

FROZEN_METHODOLOGY = {
    "rebalance_frequency": "M",
    "initial_cash": 1_000_000.0,
    "cost_scenario": "base",
    "holdout_size": 252,
    "random_seed": 20260824,
}


def _engine() -> VectorBTResearchEngine:
    return VectorBTResearchEngine(
        config=BacktestConfig(
            rebalance_frequency=FROZEN_METHODOLOGY["rebalance_frequency"],
            initial_cash=FROZEN_METHODOLOGY["initial_cash"],
            cost_model=IndiaCostModel(scenario=FROZEN_METHODOLOGY["cost_scenario"]),
        )
    )


def _zoo_family_ids() -> tuple[str, ...]:
    return tuple(entry["family_id"] for entry in ZOO_FAMILIES)


def run_world(
    world_id: str,
    *,
    campaigns: CampaignStore,
    ledger: HypothesisLedger,
    seed: int,
    output_dir: Path,
) -> dict[str, object]:
    """Run one world end-to-end; returns its summary payload."""
    world = build_world(world_id, seed=seed)
    family_ids = _zoo_family_ids()
    campaign = campaigns.create_campaign(
        objective=(
            f"world {world_id} ({world.name}): verify the framework "
            "against known truth — " + world.truth.get("signal", "none")
        ),
        strategy_families=family_ids,
        budget=ResearchBudget(
            max_trials=len(family_ids),
            max_trials_per_family=1,
            max_parameter_variants=1,
        ),
    )
    # Reserve every family BEFORE any evaluation: the DSR trial count is
    # fixed before any holdout result exists. Hypothesis ids are bulk-
    # allocated from the shared ledger counter so ids are globally unique
    # across worlds.
    hypothesis_ids: dict[str, str] = {}
    for family_id, hypothesis_id in zip(
        family_ids, ledger.next_hypothesis_ids(len(family_ids))
    ):
        campaigns.reserve_trial(
            campaign.campaign_id,
            hypothesis_id,
            family=family_id,
        )
        hypothesis_ids[family_id] = hypothesis_id

    engine = _engine()
    results = run_benchmark_zoo(
        world.market_data.close,
        fundamentals=world.fundamentals,
        membership=world.membership,
        engine=engine,
        seed=seed,
    )
    placebos = generate_placebo_results(
        world.market_data.close, engine=engine, samples=20, seed=seed
    )
    accounting = dsr_accounting_report(
        ledger, campaigns, campaign_id=campaign.campaign_id
    )
    trials = accounting["trials_for_gate"]

    gate = ResearchGate(
        random_seed=seed,
        git_commit=_git_commit(),
        dataset_fingerprint=world.fingerprint(),
    )
    family_outcomes: dict[str, dict[str, object]] = {}
    for family_id in family_ids:
        result = results.get(family_id)
        if result is None:
            campaigns.record_outcome(
                campaign.campaign_id,
                hypothesis_ids[family_id],
                status="insufficient_data",
            )
            ledger.record(
                hypothesis_id=hypothesis_ids[family_id],
                status="insufficient_data",
                hypothesis=f"zoo family {family_id} on world {world_id}",
                strategy=family_id,
                strategy_family=family_id,
                features=["close"],
                transformations=[],
                campaign_id=campaign.campaign_id,
                reason="zoo family could not run on this world's data",
            )
            family_outcomes[family_id] = {
                "status": "insufficient_data",
                "reason": "zoo family could not run on this world's data",
            }
            continue
        benchmarks = {
            other: results[other]
            for other in family_ids
            if other != family_id and other in results
        }
        # Same validation protocol as the frozen baseline: walk-forward
        # with train 252 / test 63 / purge 20 / embargo 5.
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
        decision = gate.evaluate(
            result,
            benchmarks=benchmarks,
            validation=validation,
            placebo_results=placebos,
            cost_model_name="india:base",
            rebalance_frequency=FROZEN_METHODOLOGY["rebalance_frequency"],
            validation_method="walk_forward",
            strategy_version="1.0",
            universe=f"world_{world_id}",
            trials=trials,
            trials_source="campaign",
        )
        status = "accepted" if decision.verdict == "PASS" else "rejected"
        campaigns.record_outcome(
            campaign.campaign_id, hypothesis_ids[family_id], status=status
        )
        ledger.record(
            hypothesis_id=hypothesis_ids[family_id],
            status=status,
            hypothesis=f"zoo family {family_id} on world {world_id}",
            strategy=family_id,
            strategy_family=family_id,
            features=["close"],
            transformations=[],
            parameters=dict(world.truth),
            campaign_id=campaign.campaign_id,
            metrics={
                "sharpe": float(result.metrics.sharpe),
                "annualized_return": float(result.metrics.annualized_return),
                "max_drawdown": float(result.metrics.max_drawdown),
                "turnover": float(result.metrics.turnover),
            },
            gate_result=decision.to_dict(),
            reason="; ".join(decision.reasons) if decision.reasons else "PASS",
            dataset_fingerprint=world.fingerprint(),
            config_fingerprint="synthetic-world-zoo",
            code_fingerprint=_git_commit(),
        )
        family_outcomes[family_id] = {
            "status": status,
            "verdict": decision.verdict,
            "gate_score": decision.score,
            "sharpe": float(result.metrics.sharpe),
            "annualized_return": float(result.metrics.annualized_return),
            "max_drawdown": float(result.metrics.max_drawdown),
            "turnover": float(result.metrics.turnover),
            "reasons": list(decision.reasons),
        }

    campaigns.set_status(
        campaign.campaign_id,
        CampaignStatus.COMPLETED,
        reason="zoo evaluation finished",
    )
    summary = {
        "world_id": world_id,
        "world_name": world.name,
        "description": world.description,
        "truth": dict(world.truth),
        "seed": seed,
        "campaign_id": campaign.campaign_id,
        "dataset_fingerprint": world.fingerprint(),
        "trials_corrected": trials,
        "accounting": accounting,
        "families": family_outcomes,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    world_dir = output_dir / f"world_{world_id}"
    world_dir.mkdir(parents=True, exist_ok=True)
    (world_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (world_dir / "summary.md").write_text(
        _summary_markdown(summary) + "\n", encoding="utf-8"
    )
    return summary


def run_world_g_search(
    *,
    campaigns: CampaignStore,
    ledger: HypothesisLedger,
    seed: int,
    output_dir: Path,
) -> dict[str, object]:
    """Budget-bounded random-variant search demo on world G (noise)."""
    world = build_world("G", seed=seed)
    campaign = campaigns.create_campaign(
        objective=(
            "world G: bounded random-variant search on pure noise — "
            "the budget must exhaust before any promotion"
        ),
        strategy_families=("momentum",),
        budget=ResearchBudget(
            max_trials=5, max_trials_per_family=5, max_parameter_variants=3
        ),
    )
    factory = variant_factory(world, seed=seed)
    engine = _engine()
    registry = StrategyRegistry()
    outcomes: list[dict[str, object]] = []
    try:
        for _number in range(1, 20):
            hypothesis_id = ledger.next_hypothesis_ids(1)[0]
            parameters = factory()
            signature = parameter_variant_signature(
                ResearchHypothesis(
                    strategy_family="momentum",
                    strategy_id="cross_sectional_momentum",
                    objective="random variant on noise",
                    economic_rationale="none — world G factory",
                    expected_mechanism="random",
                    features=["close"],
                    transformations=["momentum_63"],
                    parameters=parameters,
                )
            )
            campaigns.reserve_trial(
                campaign.campaign_id,
                hypothesis_id,
                family="momentum",
                variant_signature=signature,
            )
            strategy = registry.build("cross_sectional_momentum", parameters)
            data = world.market_data
            from backtest.benchmarks import benchmark_suite
            from portfolio.construction import EqualWeightConstructor

            weights = EqualWeightConstructor().construct(
                strategy.generate_signals(data), data
            )
            result = engine.run(
                data.close,
                weights,
                strategy_name="random_variant",
                universe_history=[],
            )
            sharpe = float(result.metrics.sharpe)
            trials = campaigns.require(campaign.campaign_id).trial_count
            decision = ResearchGate(random_seed=seed).evaluate(
                result,
                benchmarks=benchmark_suite(
                    data.close, weights, engine=engine, random_seed=seed
                ),
                trials=trials,
                trials_source="campaign",
            )
            status = "accepted" if decision.verdict == "PASS" else "rejected"
            campaigns.record_outcome(campaign.campaign_id, hypothesis_id, status=status)
            ledger.record(
                hypothesis_id=hypothesis_id,
                status=status,
                hypothesis="world G random momentum variant",
                strategy="cross_sectional_momentum",
                strategy_family="momentum",
                features=["close"],
                transformations=["momentum_63"],
                parameters=parameters,
                campaign_id=campaign.campaign_id,
                metrics={"sharpe": sharpe},
                gate_result=decision.to_dict(),
                reason="; ".join(decision.reasons) if decision.reasons else "PASS",
            )
            outcomes.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "parameters": parameters,
                    "sharpe": sharpe,
                    "status": status,
                }
            )
    except BudgetExhaustedError as exc:
        campaigns.set_status(
            campaign.campaign_id,
            CampaignStatus.BUDGET_EXHAUSTED,
            reason=str(exc),
        )
        outcomes.append({"budget_exhausted": str(exc)})

    summary = {
        "world_id": "G",
        "world_name": "multiple_testing",
        "truth": dict(world.truth),
        "seed": seed,
        "campaign_id": campaign.campaign_id,
        "campaign": campaigns.status_report(campaign.campaign_id),
        "outcomes": outcomes,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    world_dir = output_dir / "world_G_search"
    world_dir.mkdir(parents=True, exist_ok=True)
    (world_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return summary


def _git_commit() -> str:
    import subprocess

    try:
        output = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        )
        return output.stdout.strip()
    except Exception:
        return "unknown"


def _summary_markdown(summary: dict[str, object]) -> str:
    lines = [
        f"# Synthetic world {summary['world_id']} — {summary['world_name']}",
        "",
        summary["description"],
        "",
        "## Injected truth",
        "",
        "```json",
        json.dumps(summary["truth"], indent=2, default=str),
        "```",
        "",
        f"- seed: `{summary['seed']}`",
        f"- campaign: `{summary['campaign_id']}`",
        f"- dataset fingerprint: `{summary['dataset_fingerprint']}`",
        f"- DSR trials corrected: `{summary['trials_corrected']}`",
        "",
        "## Zoo outcomes",
        "",
        "| family | status | verdict | sharpe | ann. return | maxDD | turnover |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    families = summary["families"]
    assert isinstance(families, dict)
    for family_id, outcome in families.items():
        assert isinstance(outcome, dict)
        lines.append(
            f"| {family_id} | {outcome.get('status')} | "
            f"{outcome.get('verdict', '-')} | {outcome.get('sharpe', 0):.3f} | "
            f"{outcome.get('annualized_return', 0):.3f} | "
            f"{outcome.get('max_drawdown', 0):.3f} | {outcome.get('turnover', 0):.1f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=FROZEN_METHODOLOGY["random_seed"])
    parser.add_argument(
        "--worlds",
        default="A,B,C,D,E,F,G",
        help="comma-separated world ids to run",
    )
    args = parser.parse_args(argv)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    campaigns = CampaignStore(CAMPAIGNS_PATH)
    ledger = HypothesisLedger(LEDGER_PATH)

    selected = [item.strip().upper() for item in args.worlds.split(",")]
    unknown = [item for item in selected if item not in WORLDS]
    if unknown:
        print(f"unknown worlds: {unknown}", file=sys.stderr)
        return 2

    summaries = []
    for world_id in selected:
        print(f"[synthetic] running world {world_id} ...", flush=True)
        if world_id == "G":
            summary = run_world_g_search(
                campaigns=campaigns,
                ledger=ledger,
                seed=args.seed,
                output_dir=OUTPUT_ROOT,
            )
        else:
            summary = run_world(
                world_id,
                campaigns=campaigns,
                ledger=ledger,
                seed=args.seed,
                output_dir=OUTPUT_ROOT,
            )
        summaries.append(summary)
        print(f"[synthetic] world {world_id} done", flush=True)

    ledger_report = ledger.verify_integrity()
    campaign_report = campaigns.verify_integrity()
    print(
        json.dumps(
            {
                "worlds": summaries,
                "ledger": ledger_report,
                "campaigns": campaign_report,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )
    print(f"[synthetic] outputs under {OUTPUT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
