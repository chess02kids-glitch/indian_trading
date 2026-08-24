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
    bootstrap_sharpe_confidence_interval,
    deflated_sharpe_from_returns,
    run_walk_forward,
)
from portfolio.construction import InverseVolatilityConstructor  # noqa: E402
from research.contracts import Experiment, MarketData  # noqa: E402
from research.experiments import ExperimentManager  # noqa: E402
from research.ledger import HypothesisLedger  # noqa: E402
from research.reporting import generate_report  # noqa: E402
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "reports" / "generated"
    )
    parser.add_argument("--periods", type=int, default=756)
    parser.add_argument("--vol-target", type=float, default=0.15)
    parser.add_argument("--use-vectorbt", action="store_true")
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args(argv)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    universe = nifty_100()
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
    )
    benchmarks = benchmark_suite(data.close, result.weights, engine=engine)

    # Out-of-sample slice: last 12 months.
    oos_start = result.returns.index[-252]
    oos_period = (
        f"{oos_start.date().isoformat()}/{result.returns.index[-1].date().isoformat()}"
    )
    backtest_period = (
        f"{result.returns.index[0].date().isoformat()}/"
        f"{result.returns.index[-1].date().isoformat()}"
    )
    oos_returns = result.returns.loc[oos_start:]

    trials = 1 + len(benchmarks)
    dsr = deflated_sharpe_from_returns(oos_returns, trials)
    bootstrap = bootstrap_sharpe_confidence_interval(oos_returns, samples=1000)
    walk_forward = run_walk_forward(
        strategy,
        data,
        constructor,
        engine,
        train_size=252,
        test_size=63,
        embargo=5,
    )

    acceptance_threshold = 0.5
    rejected = dsr.probability < acceptance_threshold
    reason = None
    if rejected:
        reason = (
            f"OOS deflated Sharpe probability {dsr.probability:.3f} below "
            f"threshold {acceptance_threshold}"
        )

    # -- persistence: MLflow + ledger + reports -----------------------------
    manager = ExperimentManager(
        experiment_name="quant-india-baseline",
        tracking_dir=output_dir / "experiments",
    )
    experiment = Experiment(
        hypothesis_id="HYP-BASELINE-0001",
        strategy=strategy.name,
        parameters=strategy.parameters,
        factor_set=["momentum_3m", "quality_composite"],
        universe=universe.name,
        dataset_version=dataset_version,
        cost_model=cost_name,
    )
    record = manager.log_experiment(
        experiment,
        result=result,
        validation={
            "deflated_sharpe": dsr.to_dict(),
            "bootstrap_ci": bootstrap.to_dict(),
            "walk_forward_folds": len(walk_forward.windows),
            "oos_period": oos_period,
            "acceptance_threshold": acceptance_threshold,
        },
        benchmarks={name: benchmark for name, benchmark in benchmarks.items()},
        rejected=rejected,
        reason=reason,
        dataset_version=dataset_version,
        cost_model=cost_name,
        backtest_period=backtest_period,
        oos_period=oos_period,
    )

    ledger = HypothesisLedger(output_dir / "experiments" / "ledger.jsonl")
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
        },
        reason=reason,
        dataset_version=dataset_version,
        code_commit=record.commit_hash,
        backtest_period=backtest_period,
        oos_period=oos_period,
        cost_model=cost_name,
    )

    report = generate_report(
        result,
        benchmark_results=benchmarks,
        validation={
            "deflated_sharpe": dsr.to_dict(),
            "bootstrap_ci": bootstrap.to_dict(),
            "oos_period": oos_period,
        },
    )
    json_path, markdown_path = report.write(output_dir)

    comparison = compare_results({strategy.name: result, **benchmarks})
    summary = {
        "strategy": strategy.name,
        "universe": universe.name,
        "dataset_version": dataset_version,
        "cost_model": cost_model.to_dict(),
        "status": record.status,
        "reason": reason,
        "record": record.to_dict(),
        "full_period_metrics": result.metrics.to_dict(),
        "oos_period": oos_period,
        "deflated_sharpe": dsr.to_dict(),
        "bootstrap_ci": bootstrap.to_dict(),
        "walk_forward_folds": len(walk_forward.windows),
        "comparison": comparison.to_dict(orient="index"),
        "reports": [str(json_path), str(markdown_path)],
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
