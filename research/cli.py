"""Command-line entry points for reproducible research workflows."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from backtest import (
    BacktestConfig,
    VectorBTResearchEngine,
    run_combinatorial_purged_cv,
    run_walk_forward,
)
from backtest.validation import bootstrap_metric_intervals
from portfolio import EqualWeightConstructor

from .contracts import Experiment, MarketData, ResearchInputError
from .diagnostics import FactorDiagnostics, factor_decay, rank_stability
from .experiments import ExperimentManager
from .gate import ResearchGate, generate_placebo_results
from .reporting import (
    generate_advanced_report,
    generate_periodic_reports,
    generate_report,
)
from .runner import ResearchRun, run_strategy
from .strategies import Strategy, strategy_from_name
from .universe import Universe, resolve_universe

_VALIDATION_METHODS = ("walk_forward", "cpcv")


def _load_prices(path: Path) -> MarketData:
    if not path.is_file():
        raise ResearchInputError(f"price file does not exist: {path}")
    try:
        frame = (
            pd.read_parquet(path)
            if path.suffix.lower() == ".parquet"
            else pd.read_csv(path)
        )
    except (OSError, ValueError) as exc:
        raise ResearchInputError(f"could not read price file: {path}") from exc
    if {"date", "symbol", "close"}.issubset(frame.columns):
        return MarketData.from_long_frame(frame)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        frame = frame.set_index("date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        try:
            frame.index = pd.to_datetime(frame.index, errors="raise")
        except (TypeError, ValueError) as exc:
            raise ResearchInputError("wide price file must have a date index") from exc
    return MarketData(close=frame)


def _common_price_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--prices",
        type=Path,
        default=Path("data/clean/prices.parquet"),
        help="wide price file or canonical long-form Parquet/CSV file",
    )
    parser.add_argument(
        "--universe",
        default=None,
        help="built-in universe name or JSON universe configuration path",
    )


def _add_validation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--train-size", type=int, default=252)
    parser.add_argument("--test-size", type=int, default=63)
    parser.add_argument("--step-size", type=int, default=None)
    parser.add_argument("--purge", type=int, default=0)
    parser.add_argument("--embargo", type=int, default=0)
    parser.add_argument("--expanding", action="store_true")
    parser.add_argument(
        "--method",
        default="walk_forward",
        choices=_VALIDATION_METHODS,
    )
    parser.add_argument("--n-groups", type=int, default=6)
    parser.add_argument("--n-test-groups", type=int, default=2)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``quant-india`` research command parser."""
    parser = argparse.ArgumentParser(
        prog="quant-india", description="Quant India research CLI"
    )
    domains = parser.add_subparsers(dest="domain")
    research = domains.add_parser("research", help="run research workflows")
    research_commands = research.add_subparsers(dest="command")

    run = research_commands.add_parser("run", help="run a strategy and benchmarks")
    run.add_argument("--strategy", default="momentum")
    _common_price_argument(run)
    run.add_argument("--output-dir", type=Path, default=Path("reports/generated"))
    run.add_argument(
        "--tracking-dir", type=Path, default=Path("reports/generated/experiments")
    )
    run.add_argument("--seed", type=int, default=42)

    compare = research_commands.add_parser(
        "compare", help="compare a strategy with benchmarks"
    )
    compare.add_argument("--strategy", default="momentum")
    _common_price_argument(compare)
    compare.add_argument("--output-dir", type=Path, default=Path("reports/generated"))

    validate = research_commands.add_parser(
        "validate", help="run walk-forward or CPCV validation"
    )
    validate.add_argument("--strategy", default="momentum")
    _common_price_argument(validate)
    _add_validation_arguments(validate)

    gate = research_commands.add_parser(
        "gate", help="run the research gate on a strategy"
    )
    gate.add_argument("--strategy", default="momentum")
    _common_price_argument(gate)
    gate.add_argument("--output-dir", type=Path, default=Path("reports/generated"))
    gate.add_argument("--seed", type=int, default=42)
    gate.add_argument("--placebo-samples", type=int, default=50)
    _add_validation_arguments(gate)

    diagnostics_cmd = research_commands.add_parser(
        "diagnostics", help="compute factor diagnostics"
    )
    diagnostics_cmd.add_argument("--strategy", default="momentum")
    _common_price_argument(diagnostics_cmd)
    diagnostics_cmd.add_argument(
        "--output-dir", type=Path, default=Path("reports/generated")
    )

    replay = domains.add_parser("replay", help="long-run replay tooling")
    replay_commands = replay.add_subparsers(dest="command")
    plan = replay_commands.add_parser("plan", help="show the deterministic schedule")
    plan.add_argument("--replay-id", default="nightly")
    plan.add_argument("--start", required=True)
    plan.add_argument("--end", required=True)
    plan.add_argument("--frequency", default="B")
    plan.add_argument("--rebalance-frequency", default="M")
    plan.add_argument("--seed", type=int, default=42)
    resume = replay_commands.add_parser(
        "status", help="show replay restart-recovery status"
    )
    resume.add_argument(
        "--state-dir", type=Path, default=Path("reports/generated/replays")
    )
    resume.add_argument("--replay-id", default="nightly")

    experiments = domains.add_parser("experiments", help="inspect tracked experiments")
    experiment_commands = experiments.add_subparsers(dest="command")
    list_command = experiment_commands.add_parser(
        "list", help="list local experiment records"
    )
    list_command.add_argument(
        "--tracking-dir", type=Path, default=Path("reports/generated/experiments")
    )

    report = domains.add_parser("report", help="generate research reports")
    report_commands = report.add_subparsers(dest="command")
    generate = report_commands.add_parser(
        "generate", help="generate JSON and Markdown output"
    )
    generate.add_argument("--strategy", default="momentum")
    _common_price_argument(generate)
    generate.add_argument("--output-dir", type=Path, default=Path("reports/generated"))

    periods = report_commands.add_parser(
        "periods", help="generate daily/weekly/monthly portfolio reports"
    )
    periods.add_argument("--strategy", default="momentum")
    _common_price_argument(periods)
    periods.add_argument("--output-dir", type=Path, default=Path("reports/generated"))
    periods.add_argument(
        "--periods", nargs="+", default=["D", "W", "M"], choices=["D", "W", "M"]
    )

    advanced = report_commands.add_parser(
        "advanced", help="generate validated research reports (daily/weekly/monthly)"
    )
    advanced.add_argument("--strategy", default="momentum")
    _common_price_argument(advanced)
    advanced.add_argument("--output-dir", type=Path, default=Path("reports/generated"))
    advanced.add_argument(
        "--periods", nargs="+", default=["D", "W", "M"], choices=["D", "W", "M"]
    )
    _add_validation_arguments(advanced)

    return parser


def _resolve_cli_universe(value: str | None) -> Universe | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"nifty50", "nifty_50", "nifty100", "nifty_100"}:
        return resolve_universe({"name": normalized})
    return resolve_universe(Path(value))


def _strategy_run(args: argparse.Namespace) -> tuple[Strategy, MarketData, ResearchRun]:
    data = _load_prices(args.prices)
    strategy = strategy_from_name(args.strategy)
    universe = _resolve_cli_universe(args.universe)
    if universe is not None:
        data = data.select(universe.symbols)
    return strategy, data, run_strategy(strategy, data)


def _experiment_for(strategy: Strategy, data: MarketData) -> Experiment:
    return Experiment(
        hypothesis_id="cli",
        strategy=strategy.name,
        parameters=strategy.parameters,
        factor_set=(strategy.name,),
        universe=f"symbols:{','.join(data.close.columns)}",
    )


def _run_validation(
    strategy: Strategy,
    data: MarketData,
    args: argparse.Namespace,
) -> object:
    engine = VectorBTResearchEngine(BacktestConfig())
    if args.method == "cpcv":
        return run_combinatorial_purged_cv(
            strategy,
            data,
            EqualWeightConstructor(),
            engine,
            n_groups=args.n_groups,
            n_test_groups=args.n_test_groups,
            purge=args.purge,
            embargo=args.embargo,
        )
    return run_walk_forward(
        strategy,
        data,
        EqualWeightConstructor(),
        engine,
        train_size=args.train_size,
        test_size=args.test_size,
        step_size=args.step_size,
        expanding=args.expanding,
        purge=args.purge,
        embargo=args.embargo,
    )


def _cli_gate(args: argparse.Namespace) -> int:
    strategy, data, run = _strategy_run(args)
    engine = VectorBTResearchEngine(BacktestConfig())
    placebo = generate_placebo_results(
        data.close, engine=engine, samples=args.placebo_samples, seed=args.seed
    )
    validated = _run_validation(strategy, data, args)
    from backtest.validation import validation_consistency

    consistency = validation_consistency(validated)
    gate = ResearchGate(random_seed=args.seed)
    decision = gate.evaluate(
        run.result,
        benchmarks=run.benchmarks,
        validation=validated,
        placebo_results=placebo,
        rebalance_frequency="M",
        validation_method=args.method,
    )
    summary = {
        "gate": decision.to_dict(),
        "validation_consistency": consistency,
        "validation": validated.to_dict(),
        "comparison": run.comparison().to_dict(orient="index"),
    }
    path = decision.write(args.output_dir)
    summary_path = Path(args.output_dir) / "research_gate_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"verdict": decision.verdict, "score": decision.score, "report": str(path)},
            sort_keys=True,
        )
    )
    return 0


def _cli_diagnostics(args: argparse.Namespace) -> int:
    strategy, data, run = _strategy_run(args)
    factor_values = strategy.generate_signals(data).values
    returns = data.close.pct_change().fillna(0.0)
    diagnostics = FactorDiagnostics(
        factor_decay=factor_decay({"factor": factor_values}, returns),
        rank_stability=rank_stability({"factor": factor_values}),
        turnover_attribution=_turnover_attribution(run.result),
        volatility_contribution=_volatility_contributions(run.result, returns),
    )
    path = Path(args.output_dir) / "factor_diagnostics.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diagnostics.to_json() + "\n", encoding="utf-8")
    print(json.dumps({"path": str(path)}, sort_keys=True))
    return 0


def _turnover_attribution(result: object) -> dict:
    from research.diagnostics import turnover_attribution

    return turnover_attribution(result)


def _volatility_contributions(
    result: object, returns: pd.DataFrame
) -> dict[str, float]:
    from research.diagnostics import volatility_contribution

    return volatility_contribution(result.weights, returns)


def cli_main(argv: Sequence[str] | None = None) -> int:
    """Execute CLI arguments and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.domain is None:
        parser.print_help()
        return 0
    if args.domain == "experiments" and args.command == "list":
        records = ExperimentManager(
            mlflow_module=None, tracking_dir=args.tracking_dir
        ).list_records()
        print(
            json.dumps(
                [record.to_dict() for record in records], default=str, sort_keys=True
            )
        )
        return 0
    if args.domain == "replay" and args.command == "plan":
        from research.replay import LongRunReplay

        replay = LongRunReplay(
            Path("reports/generated/replays"),
            replay_id=args.replay_id,
            start=pd.Timestamp(args.start).date(),
            end=pd.Timestamp(args.end).date(),
            frequency=args.frequency,
            rebalance_frequency=args.rebalance_frequency,
            seed=args.seed,
        )
        print(
            json.dumps(replay.build_schedule().to_dict(), sort_keys=True, default=str)
        )
        return 0
    if args.domain == "replay" and args.command == "status":
        from research.replay import LongRunReplay

        replay = LongRunReplay(
            args.state_dir,
            replay_id=args.replay_id,
            start=pd.Timestamp("2000-01-01").date(),
            end=pd.Timestamp("2000-01-01").date(),
        )
        completed = replay.completed_steps()
        print(
            json.dumps(
                {
                    "replay_id": args.replay_id,
                    "completed_steps": [value.isoformat() for value in completed],
                    "completed_count": len(completed),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.domain == "research" and args.command in {"run", "compare"}:
        strategy, data, run = _strategy_run(args)
        report = generate_report(run.result, run.benchmarks)
        report.write(args.output_dir)
        if args.command == "run":
            manager = ExperimentManager(
                tracking_dir=args.tracking_dir,
            )
            manager.log_experiment(
                _experiment_for(strategy, data),
                result=run.result,
                benchmarks=run.benchmarks,
                random_seed=args.seed,
            )
            print(json.dumps(run.result.to_dict(), default=str, sort_keys=True))
        else:
            print(run.comparison().to_json(orient="index"))
        return 0
    if args.domain == "research" and args.command == "validate":
        data = _load_prices(args.prices)
        strategy = strategy_from_name(args.strategy)
        universe = _resolve_cli_universe(args.universe)
        if universe is not None:
            data = data.select(universe.symbols)
        result = _run_validation(strategy, data, args)
        print(json.dumps(result.to_dict(), default=str, sort_keys=True))
        return 0
    if args.domain == "research" and args.command == "gate":
        return _cli_gate(args)
    if args.domain == "research" and args.command == "diagnostics":
        return _cli_diagnostics(args)
    if args.domain == "report" and args.command == "generate":
        _, _, run = _strategy_run(args)
        report = generate_report(run.result, run.benchmarks)
        paths = report.write(args.output_dir)
        print(
            json.dumps(
                {"json": str(paths[0]), "markdown": str(paths[1])}, sort_keys=True
            )
        )
        return 0
    if args.domain == "report" and args.command == "periods":
        _, _, run = _strategy_run(args)
        period_reports = generate_periodic_reports(
            run.result, periods=tuple(args.periods)
        )
        paths = {
            period: report.write(args.output_dir)
            for period, report in period_reports.items()
        }
        print(json.dumps({k: str(v) for k, v in paths.items()}, sort_keys=True))
        return 0
    if args.domain == "report" and args.command == "advanced":
        strategy, data, run = _strategy_run(args)
        validated = _run_validation(strategy, data, args)
        from backtest.validation import validation_consistency

        consistency = validation_consistency(validated)
        gate = ResearchGate(random_seed=42)
        decision = gate.evaluate(
            run.result,
            benchmarks=run.benchmarks,
            validation=validated,
        )
        intervals = bootstrap_metric_intervals(
            run.result.returns,
            turnover=run.result.trades["turnover"],
            samples=500,
            seed=42,
        )
        paths = {}
        for frequency in args.periods:
            period_report = generate_periodic_reports(run.result, periods=(frequency,))[
                frequency
            ]
            report = generate_advanced_report(
                run.result,
                frequency=frequency,
                benchmark_results=run.benchmarks,
                validation=validated,
                gate_result=decision,
                configuration={
                    "strategy": strategy.name,
                    "parameters": dict(strategy.parameters),
                },
                confidence_intervals=intervals,
                period_report=period_report,
                metadata={
                    "validation_consistency": consistency,
                    "gate_verdict": decision.verdict,
                },
            )
            paths[frequency] = str(report.write(args.output_dir)[0])
        print(json.dumps(paths, sort_keys=True))
        return 0
    parser.error("a supported subcommand is required")
    return 2
