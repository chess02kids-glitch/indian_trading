"""Command-line entry points for reproducible research workflows."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

import pandas as pd

from backtest import BacktestConfig, VectorBTResearchEngine, run_walk_forward
from portfolio import EqualWeightConstructor

from .contracts import Experiment, MarketData, ResearchInputError
from .experiments import ExperimentManager
from .reporting import generate_report
from .runner import ResearchRun, run_strategy
from .strategies import Strategy, strategy_from_name
from .universe import Universe, resolve_universe


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

    compare = research_commands.add_parser(
        "compare", help="compare a strategy with benchmarks"
    )
    compare.add_argument("--strategy", default="momentum")
    _common_price_argument(compare)
    compare.add_argument("--output-dir", type=Path, default=Path("reports/generated"))

    validate = research_commands.add_parser(
        "validate", help="run walk-forward validation"
    )
    validate.add_argument("--strategy", default="momentum")
    _common_price_argument(validate)
    validate.add_argument("--train-size", type=int, default=252)
    validate.add_argument("--test-size", type=int, default=63)
    validate.add_argument("--step-size", type=int, default=None)
    validate.add_argument("--embargo", type=int, default=0)
    validate.add_argument("--expanding", action="store_true")

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
    if args.domain == "research" and args.command in {"run", "compare"}:
        strategy, data, run = _strategy_run(args)
        report = generate_report(run.result, run.benchmarks)
        report.write(args.output_dir)
        if args.command == "run":
            manager = ExperimentManager(tracking_dir=args.tracking_dir)
            manager.log_experiment(
                _experiment_for(strategy, data),
                result=run.result,
                benchmarks=run.benchmarks,
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
        engine = VectorBTResearchEngine(BacktestConfig())
        result = run_walk_forward(
            strategy,
            data,
            EqualWeightConstructor(),
            engine,
            train_size=args.train_size,
            test_size=args.test_size,
            step_size=args.step_size,
            expanding=args.expanding,
            embargo=args.embargo,
        )
        print(json.dumps(result.to_dict(), default=str, sort_keys=True))
        return 0
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
    parser.error("a supported subcommand is required")
    return 2
