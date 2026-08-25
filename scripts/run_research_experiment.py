"""Run the baseline research experiment end to end.

Candidate: cross-sectional 3M momentum + fundamental quality screen on the
frozen Nifty 100 research snapshot, monthly rebalance, long-only,
inverse-volatility weights, 15% volatility target, base India cost model.
Baselines: buy-and-hold, equal weight, inverse volatility, persistence,
and a seeded random placebo — all under the same universe, rebalance
frequency, and cost model.

Deterministic: synthetic data is generated from a fixed seed (no network),
so repeated runs produce identical results. Everything is written under
``reports/generated`` (git-ignored): research reports, the hypothesis
ledger, and the local MLflow store.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
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
from data.universe import load_universe_dataset  # noqa: E402
from portfolio.construction import InverseVolatilityConstructor  # noqa: E402
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
from research.reporting import (  # noqa: E402
    generate_advanced_report,
    generate_periodic_reports,
    generate_report,
)
from research.strategies import MomentumQualityStrategy  # noqa: E402
from research.universe import nifty_100  # noqa: E402


def make_synthetic_dataset(
    symbols: tuple[str, ...],
    periods: int = 756,
    start: str = "2023-01-02",
    seed: int = 20260824,
) -> pd.DataFrame:
    """Deterministic synthetic daily close prices for the universe."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, periods=periods, freq="B")
    drift = rng.normal(0.0002, 0.0005, size=len(symbols))
    vol = rng.uniform(0.012, 0.028, size=len(symbols))
    returns = rng.normal(drift, vol, size=(periods, len(symbols)))
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(close, index=index, columns=list(symbols))


def make_synthetic_fundamentals(
    symbols: tuple[str, ...],
    prices: pd.DataFrame,
    seed: int = 777,
) -> pd.DataFrame:
    """Deterministic quarterly fundamentals (ROE, debt/equity) per symbol."""
    rng = np.random.default_rng(seed)
    quarter_ends = prices.index[::63]
    rows = []
    for date in quarter_ends:
        for symbol in symbols:
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "roe": float(rng.normal(0.12, 0.06)),
                    "debt_to_equity": float(abs(rng.normal(0.8, 0.35))),
                }
            )
    return pd.DataFrame(rows)


def _dataset_fingerprint(prices: pd.DataFrame) -> str:
    """Stable content fingerprint of the price panel used by a run."""
    import hashlib

    payload = prices.round(12).to_json(orient="split", date_format="iso")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _config_fingerprint(config: BacktestConfig, strategy: object) -> str:
    """Stable fingerprint of engine configuration plus strategy parameters."""
    import hashlib

    payload = {
        "rebalance_frequency": config.rebalance_frequency,
        "initial_cash": config.initial_cash,
        "cost_model": getattr(config.cost_model, "to_dict", lambda: {})()
        if callable(getattr(config.cost_model, "to_dict", None))
        else str(config.cost_model),
        "volatility_target": config.volatility_target,
        "max_leverage": config.max_leverage,
        "strategy": getattr(strategy, "name", ""),
        "parameters": dict(getattr(strategy, "parameters", {}) or {}),
    }
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _gate_reason(decision: object) -> str:
    """Human-readable gate failure reason for ledger/metadata."""
    failures = getattr(decision, "failures", ()) or ()
    if failures:
        return "; ".join(check.message for check in failures)
    return f"research gate verdict: {getattr(decision, 'verdict', 'unknown')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "reports" / "generated"
    )
    parser.add_argument("--periods", type=int, default=756)
    parser.add_argument("--holdout-size", type=int, default=252)
    parser.add_argument("--vol-target", type=float, default=0.15)
    parser.add_argument("--use-vectorbt", action="store_true")
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args(argv)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    # Versioned universe: the frozen research snapshot must match the
    # versioned constituent CSV (data/universe/nifty100.csv), and the
    # snapshot validity window is recorded in every experiment artifact.
    universe_dataset = load_universe_dataset()
    snapshot_start, _ = universe_dataset.valid_window("nifty100")
    universe = nifty_100(as_of=snapshot_start)
    if set(universe.symbols) != set(universe_dataset.all_symbols("nifty100")):
        raise SystemExit(
            "frozen nifty100 research snapshot does not match the versioned "
            "constituent CSV; refusing to run on an ambiguous universe"
        )
    universe_version = f"nifty100-snapshot-{snapshot_start.isoformat()}"
    prices = make_synthetic_dataset(
        universe.symbols, periods=args.periods, seed=args.seed
    )
    fundamentals = make_synthetic_fundamentals(universe.symbols, prices)
    data = MarketData(close=prices)
    dataset_version = (
        f"synthetic-nifty100-v1 (seed={args.seed}, periods={args.periods})"
    )

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
    )
    constructor = InverseVolatilityConstructor(window=20)

    result = engine.run(
        data.close,
        constructor.construct(strategy.generate_signals(data), data),
        strategy_name=strategy.name,
        universe_history=universe.history,
    )
    # Full-period benchmarks (report context; the gate compares holdout
    # slices below so every comparison is on the same locked evidence).
    benchmarks = benchmark_suite(data.close, result.weights, engine=engine)

    # -- locked holdout protocol (TRAIN -> VALIDATION -> LOCKED HOLDOUT) ----
    # The trailing ``--holdout-size`` observations are locked before any
    # validation runs: walk-forward and CPCV only ever see the development
    # prefix (guarded explicitly), and the candidate receives exactly one
    # final evaluation on the untouched holdout slice.
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
        universe_history=universe.history,
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
    holdout_period = (
        f"{split.holdout_start.date().isoformat()}/"
        f"{split.holdout_end.date().isoformat()}"
    )
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
    # Like-for-like holdout comparisons: every benchmark and placebo is
    # evaluated on the same locked holdout slice, same cost model, same
    # rebalance schedule, and same position constraints as the candidate.
    holdout_benchmarks = {
        name: engine.run(
            holdout_prices,
            benchmark_result.weights.loc[split.holdout_index],
            strategy_name=f"{name}_holdout",
            universe_history=universe.history,
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

    acceptance_threshold = 0.5
    rejected = dsr.probability < acceptance_threshold
    reason = None
    if rejected:
        reason = (
            f"OOS deflated Sharpe probability {dsr.probability:.3f} below "
            f"threshold {acceptance_threshold}"
        )
    if reason is None and gate_decision.verdict == "FAIL":
        reason = _gate_reason(gate_decision)

    # -- report warnings and limitations (spec: explicit, machine-readable) -
    # Warnings: gate checks that passed with caveats (never silent).
    # Limitations: fixed, honest dataset/universe caveats — no alpha claim.
    warnings = [
        check.message for check in gate_decision.checks if check.status == "warn"
    ]
    limitations = [
        "synthetic price dataset: results validate the research pipeline, "
        "they are not evidence about real Indian equities",
        f"universe is a single-date snapshot ({universe_version}): results "
        "carry a survivorship-bias limitation",
        "historical corporate-action adjustment is not wired to a real data source",
        "no alpha or statistical-significance claim is made",
    ]

    # -- cost-scenario survival table (OPTIMISTIC / BASE / PESSIMISTIC) -----
    # Reporting only: the gate above evaluates the pre-declared base
    # scenario. The same weights are re-simulated under each scenario's
    # spread/slippage assumptions so cost fragility is visible per period.
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
                universe_history=universe.history,
            )
        )
        scenario_holdout = (
            holdout_result
            if scenario == "base"
            else scenario_engine.run(
                holdout_prices,
                result.weights.loc[split.holdout_index],
                strategy_name=f"{strategy.name}_{scenario}_holdout",
                universe_history=universe.history,
            )
        )
        scenario_results[scenario] = {
            "cost_model": scenario_model.to_dict(),
            "full_period": scenario_full.metrics.to_dict(),
            "holdout": scenario_holdout.metrics.to_dict(),
        }

    # -- reports, diagnostics, and artifacts (before tracking) --------------
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
            "universe_version": universe_version,
            "universe_symbols": list(universe.symbols),
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
        },
    )
    json_path, markdown_path = report.write(output_dir)

    # Periodic (daily/weekly/monthly) portfolio reports with exposure,
    # turnover, drawdown, and factor exposure of held positions.
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
                "dataset_fingerprint": _dataset_fingerprint(prices),
            },
        )
        advanced_paths[frequency] = str(advanced.write(output_dir)[0])

    # Factor diagnostics for the researched factor panel.
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

    # -- persistence: MLflow + ledger -----------------------------------------
    manager = ExperimentManager(
        experiment_name="quant-india-baseline",
        tracking_dir=output_dir / "experiments",
    )
    ledger = HypothesisLedger(output_dir / "experiments" / "ledger.jsonl")
    # Allocate the next incremental hypothesis id (HYP-00001, HYP-00002, ...)
    # so every run — including re-runs — records a distinct, auditable
    # experiment instead of overwriting the previous one.
    hypothesis = ledger.next_hypothesis_id()
    experiment = Experiment(
        hypothesis_id=hypothesis,
        strategy=strategy.name,
        parameters=strategy.parameters,
        factor_set=["momentum_3m", "quality_composite"],
        universe=universe.name,
        dataset_version=dataset_version,
        cost_model=cost_name,
    )

    # MLflow artifact set (equity, drawdown, CIs, validation, weights,
    # diagnostics, report).
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
        dataset_fingerprint=_dataset_fingerprint(prices),
        gate_result=gate_decision,
        artifacts=artifacts,
    )

    ledger.for_experiment(
        experiment,
        status=record.status,
        hypothesis_text=(
            "3M momentum + quality on Nifty 100, monthly, long-only, vol-targeted"
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
        dataset_fingerprint=_dataset_fingerprint(prices),
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
        "reports": [str(json_path), str(markdown_path)],
        "periodic_reports": {k: str(v) for k, v in period_paths.items()},
        "advanced_reports": {k: str(v) for k, v in advanced_paths.items()},
        "factor_diagnostics": str(diagnostics_path),
        "artifacts": artifacts,
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
