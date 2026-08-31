"""Run the EXACT frozen v0.6 baseline on real NSE data (v0.7).

Same strategy, parameters, rebalance schedule, cost assumptions,
holdout/walk-forward/CPCV methodology, DSR calculation and research gate
as ``scripts/run_research_experiment.py`` (v0.6, locked — not modified).
Only the *data* differs:

* prices: validated eod2_data panel (split/bonus-adjusted, NSE official
  daily reports mirror) over the maximum clean overlapping period;
* universe: point-in-time Nifty 100 membership (CC BY 4.0 source) — the
  frozen cross-sectional screens rank only within each date's actual
  members (``MomentumQualityStrategy.active_members``);
* fundamentals: operator bundle (yfinance quarterly ROE / debt-to-equity,
  one-quarter conservative publication lag).

The frozen configuration is asserted explicitly against the locked v0.6
values before any engine run; any drift aborts the experiment.

Deterministic given the data snapshot, the committed PIT universe, the
operator bundle and the code commit. Outputs under
``reports/generated/real_data`` (git-ignored); the ledger entry is
appended to the shared ``reports/generated/experiments/ledger.jsonl``
(next id after the v0.6 HYP-00001 entry — never overwritten).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.benchmarks import benchmark_suite, compare_results  # noqa: E402
from backtest.costs import IndiaCostModel  # noqa: E402
from backtest.engine import BacktestConfig, VectorBTResearchEngine  # noqa: E402
from backtest.validation import (  # noqa: E402
    bootstrap_metric_intervals,
    bootstrap_sharpe_confidence_interval,
    deflated_sharpe_from_returns,
    run_holdout_protocol,
    validation_consistency,
)
from data.dataset import CleanDataCatalog  # noqa: E402
from data.universe import UniverseDataset  # noqa: E402
from ingestion.eod2_adapter import Eod2SourceSpec  # noqa: E402
from ingestion.nse_membership_adapter import (  # noqa: E402
    NseMembershipSpec,
    membership_fingerprint,
)
from portfolio.construction import InverseVolatilityConstructor  # noqa: E402
from research import realdata  # noqa: E402
from research.contracts import Experiment, MarketData  # noqa: E402
from research.diagnostics import (  # noqa: E402
    FactorDiagnostics,
    factor_decay,
    rank_stability,
    turnover_attribution,
    volatility_contribution,
)
from research.experiments import (  # noqa: E402
    ExperimentManager,
    build_research_artifacts,
)
from research.factors import MomentumFactor  # noqa: E402
from research.gate import ResearchGate, generate_placebo_results  # noqa: E402
from research.ledger import HypothesisLedger  # noqa: E402
from research.realdata import (  # noqa: E402
    build_active_membership_panel,
    build_market_panels,
    load_fundamentals_bundle,
    real_data_dataset_version,
)
from research.reporting import (  # noqa: E402
    generate_advanced_report,
    generate_periodic_reports,
    generate_report,
)
from research.strategies import MomentumQualityStrategy  # noqa: E402
from research.universe import build_universe_from_dataset  # noqa: E402

# Frozen configuration helpers — imported from the v0.6 script itself so
# the fingerprint definitions can never drift.
from scripts.run_research_experiment import (  # noqa: E402
    _config_fingerprint,
    _dataset_fingerprint,
    _gate_reason,
)

#: The locked v0.6 baseline values (v0.7 §1/§11). Asserted, not assumed.
FROZEN = {
    "momentum_lookback": 63,
    "momentum_quantile": 0.25,
    "quality_quantile": 0.5,
    "rebalance_frequency": "M",
    "initial_cash": 1_000_000.0,
    "cost_scenario": "base",
    "volatility_target": 0.15,
    "max_leverage": 1.0,
    "constructor_window": 20,
    "holdout_size": 252,
    "train_size": 252,
    "test_size": 63,
    "purge": 20,
    "embargo": 5,
    "cpcv_n_groups": 6,
    "cpcv_n_test_groups": 2,
    "acceptance_threshold": 0.5,
    "random_seed": 20260824,
}


def _assert_frozen_config(
    engine_config: BacktestConfig,
    strategy: MomentumQualityStrategy,
    constructor: InverseVolatilityConstructor,
) -> None:
    """Refuse to run if any locked v0.6 parameter drifted."""
    checks = {
        "momentum_lookback": (strategy.momentum_lookback, FROZEN["momentum_lookback"]),
        "momentum_quantile": (strategy.momentum_quantile, FROZEN["momentum_quantile"]),
        "quality_quantile": (strategy.quality_quantile, FROZEN["quality_quantile"]),
        "rebalance_frequency": (
            engine_config.rebalance_frequency,
            FROZEN["rebalance_frequency"],
        ),
        "initial_cash": (engine_config.initial_cash, FROZEN["initial_cash"]),
        "cost_scenario": (
            engine_config.cost_model.scenario,
            FROZEN["cost_scenario"],
        ),
        "volatility_target": (
            engine_config.volatility_target,
            FROZEN["volatility_target"],
        ),
        "max_leverage": (engine_config.max_leverage, FROZEN["max_leverage"]),
        "constructor_window": (constructor.window, FROZEN["constructor_window"]),
    }
    drifted = {
        name: {"actual": actual, "frozen": frozen}
        for name, (actual, frozen) in checks.items()
        if actual != frozen
    }
    if drifted:
        raise SystemExit(f"frozen v0.6 configuration drifted: {drifted}")


def _nifty100_dir(universe_dir: Path) -> Path:
    """Resolve the Nifty 100 PIT directory.

    ``ingest_real_data.py`` writes per-index subdirectories
    (``{slug}-pit/{slug}.csv``); the repository's flat layout
    (``data/universe/nifty100.csv``) is still accepted.
    """
    sub = Path(universe_dir) / "nifty100-pit"
    return sub if (sub / "nifty100.csv").is_file() else Path(universe_dir)


def _load_real_data(
    *,
    as_of: str,
    window_start: str,
    bundle_dir: Path,
    universe_dir: Path,
) -> dict:
    """Load panel + PIT universe + active-membership mask + fundamentals."""
    catalog = CleanDataCatalog()
    n100_dir = _nifty100_dir(universe_dir)
    if not (n100_dir / "nifty100.csv").is_file():
        raise SystemExit(
            "point-in-time universe missing; run: "
            "python scripts/ingest_real_data.py --local ..."
        )
    dataset = UniverseDataset.from_dir(n100_dir)
    requested = realdata.requested_constituents(
        dataset, window_start=window_start, as_of=as_of
    )
    panels = build_market_panels(
        catalog,
        requested,
        source="eod2_data",
        window_start=window_start,
        window_end=as_of,
    )
    active_members = build_active_membership_panel(
        dataset,
        "nifty100",
        calendar=panels.window.index,
        symbols=panels.symbols,
    )
    try:
        fundamentals, fundamentals_provenance = load_fundamentals_bundle(
            bundle_dir, as_of=as_of
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "INSUFFICIENT_DATA: "
            f"{exc}\n"
            "The frozen baseline's quality screen requires real quarterly "
            "fundamentals, which cannot be fetched from Arena (network is "
            "restricted to PyPI/GitHub). Operator action (single "
            "external-data command, see docs/real_data.md):\n"
            "  python scripts/ingest_real_data.py --fetch-fundamentals\n"
            "then re-run this script (or merge with --from-bundle first)."
        ) from exc

    membership_csv = n100_dir / "nifty100.csv"
    provenance = {}
    provenance_path = n100_dir / "provenance.json"
    if provenance_path.is_file():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    membership_spec = NseMembershipSpec(
        commit=str(provenance.get("source", {}).get("commit", ""))
    )

    # eod2 source pin: from the completeness report written by the local
    # ingestion step (degrades to "unknown" if the report is absent).
    eod2_commit = ""
    completeness_path = (
        ROOT / "reports" / "generated" / "real_data" / "completeness_report.json"
    )
    if completeness_path.is_file():
        try:
            payload = json.loads(completeness_path.read_text(encoding="utf-8"))
            eod2_commit = str(
                payload.get("prices", {}).get("source", {}).get("commit", "")
            )
        except (json.JSONDecodeError, OSError):
            eod2_commit = ""
    eod2_spec = Eod2SourceSpec(commit=eod2_commit)
    return {
        "catalog": catalog,
        "dataset": dataset,
        "panels": panels,
        "active_members": active_members,
        "fundamentals": fundamentals,
        "fundamentals_provenance": fundamentals_provenance,
        "eod2_spec": eod2_spec,
        "membership_spec": membership_spec,
        "membership_fingerprint": membership_fingerprint(pd.read_csv(membership_csv)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "reports" / "generated" / "real_data",
    )
    parser.add_argument("--as-of", default="2026-08-25")
    parser.add_argument("--window-start", default="2015-01-01")
    parser.add_argument(
        "--universe-dir",
        type=Path,
        default=ROOT / "data" / "universe" / "nifty100-pit",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=ROOT / "data" / "bundle",
    )
    parser.add_argument(
        "--ledger-path",
        type=Path,
        default=ROOT / "reports" / "generated" / "experiments" / "ledger.jsonl",
    )
    parser.add_argument("--holdout-size", type=int, default=FROZEN["holdout_size"])
    parser.add_argument("--vol-target", type=float, default=FROZEN["volatility_target"])
    parser.add_argument("--use-vectorbt", action="store_true")
    parser.add_argument("--seed", type=int, default=FROZEN["random_seed"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="load + validate everything, assert the frozen config, print the "
        "run plan — do not execute the backtest",
    )
    args = parser.parse_args(argv)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.holdout_size != FROZEN["holdout_size"]:
        raise SystemExit(
            f"--holdout-size {args.holdout_size} != frozen {FROZEN['holdout_size']}"
        )
    if args.vol_target != FROZEN["volatility_target"]:
        raise SystemExit(
            f"--vol-target {args.vol_target} != frozen {FROZEN['volatility_target']}"
        )
    if args.seed != FROZEN["random_seed"]:
        raise SystemExit(f"--seed {args.seed} != frozen {FROZEN['random_seed']}")

    # -- real data assembly -------------------------------------------------
    context = _load_real_data(
        as_of=args.as_of,
        window_start=args.window_start,
        bundle_dir=args.bundle_dir,
        universe_dir=args.universe_dir,
    )
    panels = context["panels"]
    dataset = context["dataset"]
    active_members = context["active_members"]
    fundamentals = context["fundamentals"]

    universe = build_universe_from_dataset(
        dataset,
        "nifty100",
        as_of=panels.window.start,
        metadata={
            "universe_kind": "point_in_time",
            "membership_fingerprint": context["membership_fingerprint"],
        },
    )
    universe_version = f"nifty100-pit-{context['membership_fingerprint'][:12]}"
    price_fp = panels.close.round(12)
    dataset_version = real_data_dataset_version(
        context["eod2_spec"],
        context["membership_spec"],
        fundamentals_fingerprint=context["fundamentals_provenance"].get(
            "bundle_fingerprint", "unknown"
        ),
    )
    data = MarketData(close=panels.close)

    cost_model = IndiaCostModel(scenario="base")
    cost_name = f"india:{cost_model.scenario}"
    engine_config = BacktestConfig(
        rebalance_frequency="M",
        initial_cash=1_000_000.0,
        cost_model=cost_model,
        volatility_target=args.vol_target,
        use_vectorbt=args.use_vectorbt,
    )
    engine = VectorBTResearchEngine(engine_config)
    strategy = MomentumQualityStrategy(
        momentum_lookback=63,
        momentum_quantile=0.25,
        quality_quantile=0.5,
        fundamentals=fundamentals,
        active_members=active_members,
    )
    constructor = InverseVolatilityConstructor(window=20)
    _assert_frozen_config(engine_config, strategy, constructor)

    plan = {
        "status": "dry_run" if args.dry_run else "running",
        "dataset_version": dataset_version,
        "universe_version": universe_version,
        "window": panels.window.to_dict(),
        "panel_symbols": len(panels.symbols),
        "excluded_symbols": dict(panels.excluded),
        "fundamentals_rows": int(len(fundamentals)),
        "fundamentals_symbols": int(fundamentals["symbol"].nunique()),
        "holdout_size": args.holdout_size,
        "frozen_config": FROZEN,
        "frozen_config_asserted": True,
    }
    if args.dry_run:
        print(json.dumps(plan, indent=2, default=str))
        return 0

    # -- EXACT v0.6 baseline (same code path as the frozen script) ----------
    universe_history = dataset.to_frame().to_dict("records")
    result = engine.run(
        data.close,
        constructor.construct(strategy.generate_signals(data), data),
        strategy_name=strategy.name,
        universe_history=universe_history,
    )
    benchmarks = benchmark_suite(data.close, result.weights, engine=engine)

    protocol = run_holdout_protocol(
        strategy,
        data,
        constructor,
        engine,
        args.holdout_size,
        train_size=252,
        test_size=63,
        purge=20,
        embargo=5,
        cpcv_n_groups=6,
        cpcv_n_test_groups=2,
        universe_history=universe_history,
    )
    split = protocol.split
    walk_forward = protocol.walk_forward
    cpcv = protocol.cpcv
    holdout_result = protocol.holdout_result
    holdout_prices = data.close.loc[split.holdout_index]

    backtest_period = (
        f"{data.close.index[0].date().isoformat()}/"
        f"{data.close.index[-1].date().isoformat()}"
    )
    dev_period = (
        f"{split.dev_start.date().isoformat()}/{split.dev_end.date().isoformat()}"
    )
    holdout_period = f"{split.holdout_start.date().isoformat()}/{split.holdout_end.date().isoformat()}"
    oos_returns = holdout_result.returns
    oos_period = holdout_period

    trials = 1 + len(benchmarks)
    dsr = deflated_sharpe_from_returns(oos_returns, trials)
    bootstrap = bootstrap_sharpe_confidence_interval(oos_returns, samples=1000)
    intervals = bootstrap_metric_intervals(
        result.returns,
        turnover=result.trades["turnover"],
        samples=1000,
        seed=args.seed,
    )
    holdout_benchmarks = {
        name: engine.run(
            holdout_prices,
            benchmark_result.weights.loc[split.holdout_index],
            strategy_name=f"{name}_holdout",
            universe_history=universe_history,
        )
        for name, benchmark_result in benchmarks.items()
    }
    wf_consistency = validation_consistency(walk_forward)
    cpcv_consistency = validation_consistency(cpcv)
    placebos = generate_placebo_results(
        holdout_prices, engine=engine, samples=50, seed=args.seed
    )
    gate = ResearchGate(random_seed=args.seed)
    gate_decision = gate.evaluate(
        result,
        benchmarks=holdout_benchmarks,
        validation=walk_forward,
        placebo_results=placebos,
        oos_returns=oos_returns,
        cost_model_name=cost_name,
        rebalance_frequency=engine_config.rebalance_frequency,
        validation_method="walk_forward",
        universe=universe.name,
    )
    gate_path = gate_decision.write(output_dir)
    gate_summary = {
        "verdict": gate_decision.verdict,
        "score": gate_decision.score,
        "checks": [check.to_dict() for check in gate_decision.checks],
        "reproducibility": gate_decision.reproducibility,
        "holdout_boundaries": split.to_dict(),
        "path": str(gate_path),
    }
    (output_dir / "research_gate_summary.json").write_text(
        json.dumps(gate_summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )

    acceptance_threshold = FROZEN["acceptance_threshold"]
    rejected = dsr.probability < acceptance_threshold
    reason = None
    if rejected:
        reason = (
            f"OOS deflated Sharpe probability {dsr.probability:.3f} below "
            f"threshold {acceptance_threshold}"
        )
    if reason is None and gate_decision.verdict == "FAIL":
        reason = _gate_reason(gate_decision)

    warnings = [
        check.message for check in gate_decision.checks if check.status == "warn"
    ]
    limitations = [
        "real-data run: results are evidence about the research framework on "
        "real Indian equities, but NOT an alpha claim (a pass would still "
        "require independent review per the research gate)",
        "price source eod2_data carries no license file (research-only usage "
        "of a mirror of NSE official public daily reports; see "
        "docs/real_data.md)",
        "daily/ series is split/bonus-adjusted per the upstream README; the "
        "independent yfinance cross-check lives in the operator bundle "
        "(data/bundle/crosscheck_yfinance.json) — review its mismatches",
        "HDFC (member 2023-01-01→2023-07-13) has no price file in the "
        "source (delisted at the HDFC/HDFC Bank merger); excluded with "
        "reason, HDFCBANK (a member throughout) covers the bank exposure",
        "seven post-window-start IPOs (JIOFIN, BAJAJHFL, HYUNDAI, SWIGGY, "
        "ENRIN, TATACAP, TMCV) lack pre-listing prices inside the window; "
        "they are members in the PIT universe but not in the rectangular "
        "price panel (excluded with reason)",
        "point-in-time membership is stable after 2026-03-31 (next NSE "
        "reconstitution October 2026); the source coverage date is "
        "2026-05-15 (recorded, verified against the upstream snapshot)",
        "fundamentals use a conservative one-quarter publication lag "
        "(availability at next quarter end) — no publication look-ahead, "
        "but availability is coarser than reality",
        "synthetic v0.6 results are a framework test only; the synthetic "
        "and real results are NOT comparable performance estimates",
    ]

    # -- cost-scenario survival table (reporting only, as in v0.6) ----------
    scenario_results = {}
    for scenario in ("optimistic", "base", "pessimistic"):
        scenario_model = IndiaCostModel(scenario=scenario)
        scenario_engine = VectorBTResearchEngine(
            BacktestConfig(
                rebalance_frequency="M",
                initial_cash=1_000_000.0,
                cost_model=scenario_model,
                volatility_target=args.vol_target,
                use_vectorbt=args.use_vectorbt,
            )
        )
        scenario_full = (
            result
            if scenario == "base"
            else scenario_engine.run(
                data.close,
                result.weights,
                strategy_name=f"{strategy.name}_{scenario}",
                universe_history=universe_history,
            )
        )
        scenario_holdout = (
            holdout_result
            if scenario == "base"
            else scenario_engine.run(
                holdout_prices,
                result.weights.loc[split.holdout_index],
                strategy_name=f"{strategy.name}_{scenario}_holdout",
                universe_history=universe_history,
            )
        )
        scenario_results[scenario] = {
            "cost_model": scenario_model.to_dict(),
            "full_period": scenario_full.metrics.to_dict(),
            "holdout": scenario_holdout.metrics.to_dict(),
        }

    # -- reports, diagnostics, artifacts (same structure as v0.6) -----------
    from research.experiments import _commit_hash

    code_commit = _commit_hash()
    report = generate_report(
        result,
        benchmark_results=benchmarks,
        validation={
            "deflated_sharpe": dsr.to_dict(),
            "bootstrap_ci": bootstrap.to_dict(),
            "oos_period": oos_period,
            "walk_forward": walk_forward.to_dict(),
            "cpcv": cpcv.to_dict() if cpcv is not None else None,
            "holdout": {
                "boundaries": split.to_dict(),
                "metrics": holdout_result.metrics.to_dict(),
            },
        },
        metadata={
            "strategy_parameters": strategy.parameters,
            "universe": universe.name,
            "universe_kind": "point_in_time",
            "universe_version": universe_version,
            "universe_symbols_panel": list(panels.symbols),
            "universe_excluded": dict(panels.excluded),
            "dataset_version": dataset_version,
            "cost_model": cost_model.to_dict(),
            "backtest_period": backtest_period,
            "dev_period": dev_period,
            "oos_period": oos_period,
            "holdout_period": holdout_period,
            "holdout_boundaries": split.to_dict(),
            "cost_scenario_results": scenario_results,
            "warnings": warnings,
            "limitations": limitations,
            "random_seed": args.seed,
            "code_commit": code_commit,
            "mlflow_run_id": "pending",
            "frozen_config": FROZEN,
            "config_fingerprint": _config_fingerprint(engine_config, strategy),
        },
    )
    json_path, markdown_path = report.write(output_dir)

    momentum_panel = MomentumFactor(strategy.momentum_lookback).compute(data)
    period_reports = generate_periodic_reports(
        result, factor_values=momentum_panel, periods=("D", "W", "M")
    )
    period_paths = {
        period: pr.write(output_dir) for period, pr in period_reports.items()
    }
    advanced_paths = {}
    for frequency in ("D", "W", "M"):
        advanced = generate_advanced_report(
            result,
            frequency=frequency,
            benchmark_results=benchmarks,
            validation=walk_forward,
            gate_result=gate_decision,
            configuration={
                "strategy": strategy.name,
                "parameters": dict(strategy.parameters),
                "universe": universe.name,
                "cost_model": cost_model.to_dict(),
                "rebalance_frequency": engine_config.rebalance_frequency,
            },
            confidence_intervals=intervals,
            period_report=period_reports[frequency],
            metadata={
                "walk_forward_consistency": wf_consistency,
                "cpcv_consistency": cpcv_consistency,
                "universe_version": universe_version,
                "dev_period": dev_period,
                "holdout_period": holdout_period,
                "holdout_boundaries": split.to_dict(),
                "cost_scenario_results": scenario_results,
                "warnings": warnings,
                "limitations": limitations,
                "random_seed": args.seed,
                "code_commit": code_commit,
                "dataset_fingerprint": _dataset_fingerprint(price_fp),
            },
        )
        advanced_paths[frequency] = str(advanced.write(output_dir)[0])

    factor_values = MomentumFactor(strategy.momentum_lookback).compute(data)
    diagnostics = FactorDiagnostics(
        factor_decay=factor_decay(
            {"momentum_3m": factor_values}, data.close.pct_change().fillna(0.0)
        ),
        rank_stability=rank_stability({"momentum_3m": factor_values}),
        turnover_attribution=turnover_attribution(result),
        volatility_contribution=volatility_contribution(
            result.weights, data.close.pct_change().fillna(0.0)
        ),
    )
    diagnostics_path = output_dir / "factor_diagnostics.json"
    diagnostics_path.write_text(diagnostics.to_json() + "\n", encoding="utf-8")

    # -- persistence: MLflow + shared ledger (HYP continues after v0.6) ----
    manager = ExperimentManager(
        experiment_name="quant-india-baseline-realdata",
        tracking_dir=output_dir / "experiments",
    )
    ledger_path = args.ledger_path
    ledger = HypothesisLedger(ledger_path)
    hypothesis = ledger.next_hypothesis_id()
    if hypothesis == "HYP-00001":
        raise SystemExit(
            "refusing to allocate HYP-00001 for the v0.7 real-data "
            "experiment: the shared ledger does not yet contain the v0.6 "
            "entry. Run `python scripts/run_research_experiment.py` first "
            "(reproduces HYP-00001), then re-run this script so the real-"
            "data entry is recorded as HYP-00002 (v0.7 §21: never overwrite "
            "the v0.6 entry)."
        )
    experiment = Experiment(
        hypothesis_id=hypothesis,
        strategy=strategy.name,
        parameters=strategy.parameters,
        factor_set=["momentum_3m", "quality_composite"],
        universe=universe.name,
        dataset_version=dataset_version,
        cost_model=cost_name,
    )

    artifacts = build_research_artifacts(
        result,
        artifact_dir=output_dir / "experiments" / "artifacts",
        experiment_id=experiment.experiment_id,
        benchmarks=benchmarks,
        validation={
            "deflated_sharpe": dsr.to_dict(),
            "walk_forward": walk_forward.to_dict(),
            "cpcv": cpcv.to_dict(),
            "confidence_intervals": {
                name: interval.to_dict() for name, interval in intervals.items()
            },
            "oos_period": oos_period,
        },
        confidence_intervals=intervals,
        gate_result=gate_decision,
        factor_diagnostics=diagnostics.to_dict(),
        research_report=report,
    )

    record = manager.log_experiment(
        experiment,
        result=result,
        validation={
            "deflated_sharpe": dsr.to_dict(),
            "bootstrap_ci": bootstrap.to_dict(),
            "walk_forward_folds": len(walk_forward.windows),
            "cpcv_folds": len(cpcv.windows) if cpcv is not None else 0,
            "walk_forward_consistency": wf_consistency,
            "cpcv_consistency": cpcv_consistency,
            "oos_period": oos_period,
            "holdout_period": holdout_period,
            "holdout_boundaries": split.to_dict(),
            "holdout_metrics": holdout_result.metrics.to_dict(),
            "acceptance_threshold": acceptance_threshold,
        },
        benchmarks={name: benchmark for name, benchmark in benchmarks.items()},
        rejected=rejected or gate_decision.verdict == "FAIL",
        reason=reason or _gate_reason(gate_decision),
        dataset_version=dataset_version,
        cost_model=cost_name,
        backtest_period=backtest_period,
        oos_period=oos_period,
        strategy_version="1.0",
        factor_versions={
            "momentum_3m": "1.0",
            "quality_composite": "1.0",
        },
        validation_method="walk_forward",
        random_seed=args.seed,
        dataset_fingerprint=_dataset_fingerprint(price_fp),
        gate_result=gate_decision,
        artifacts=artifacts,
    )

    ledger.for_experiment(
        experiment,
        status=record.status,
        hypothesis_text=(
            "v0.7: frozen v0.6 baseline (3M momentum + quality, monthly, "
            "long-only, vol-targeted) re-run on real NSE data with "
            "point-in-time Nifty 100 membership"
        ),
        metrics={
            "oos_sharpe_probability": dsr.probability,
            "total_return": result.metrics.total_return,
            "max_drawdown": result.metrics.max_drawdown,
            "holdout_total_return": holdout_result.metrics.total_return,
            "holdout_sharpe": holdout_result.metrics.sharpe,
            "holdout_max_drawdown": holdout_result.metrics.max_drawdown,
            "holdout_turnover": holdout_result.metrics.turnover,
            "walk_forward_positive_fold_fraction": wf_consistency[
                "positive_fold_fraction"
            ],
            "pessimistic_holdout_sharpe": scenario_results["pessimistic"]["holdout"][
                "sharpe"
            ],
        },
        reason=reason,
        dataset_version=dataset_version,
        code_commit=record.commit_hash,
        backtest_period=backtest_period,
        oos_period=oos_period,
        holdout_period=holdout_period,
        universe_version=universe_version,
        cost_model=cost_name,
        dataset_fingerprint=_dataset_fingerprint(price_fp),
        config_fingerprint=_config_fingerprint(engine_config, strategy),
        code_fingerprint=record.commit_hash,
        run_id=record.run_id,
        gate_result=gate_decision.to_dict(),
    )

    comparison = compare_results({strategy.name: result, **benchmarks})
    holdout_comparison = compare_results(
        {strategy.name: holdout_result, **holdout_benchmarks}
    )
    summary = {
        "strategy": strategy.name,
        "universe": universe.name,
        "universe_kind": "point_in_time",
        "universe_version": universe_version,
        "dataset_version": dataset_version,
        "cost_model": cost_model.to_dict(),
        "status": record.status,
        "reason": reason,
        "record": record.to_dict(),
        "full_period_metrics": result.metrics.to_dict(),
        "backtest_period": backtest_period,
        "dev_period": dev_period,
        "holdout_period": holdout_period,
        "holdout_boundaries": split.to_dict(),
        "holdout_metrics": holdout_result.metrics.to_dict(),
        "cost_scenario_results": scenario_results,
        "holdout_benchmarks": {
            name: r.metrics.to_dict() for name, r in holdout_benchmarks.items()
        },
        "oos_period": oos_period,
        "deflated_sharpe": dsr.to_dict(),
        "bootstrap_ci": bootstrap.to_dict(),
        "confidence_intervals": {
            name: interval.to_dict() for name, interval in intervals.items()
        },
        "walk_forward_folds": len(walk_forward.windows),
        "cpcv_folds": len(cpcv.windows),
        "walk_forward_consistency": wf_consistency,
        "cpcv_consistency": cpcv_consistency,
        "research_gate": gate_decision.to_dict(),
        "comparison": comparison.to_dict(orient="index"),
        "holdout_comparison": holdout_comparison.to_dict(orient="index"),
        "warnings": warnings,
        "limitations": limitations,
        "frozen_config": FROZEN,
        "config_fingerprint": _config_fingerprint(engine_config, strategy),
        "panel_symbols": len(panels.symbols),
        "excluded_symbols": dict(panels.excluded),
        "reports": [str(json_path), str(markdown_path)],
        "periodic_reports": {k: str(v) for k, v in period_paths.items()},
        "advanced_reports": {k: str(v) for k, v in advanced_paths.items()},
        "factor_diagnostics": str(diagnostics_path),
        "artifacts": artifacts,
        "ledger_path": str(ledger_path),
        "hypothesis_id": hypothesis,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    summary_path = output_dir / "baseline_experiment_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
