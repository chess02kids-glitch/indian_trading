"""Backend API bridging the research cockpit to the existing research engine.

This module is the *only* new orchestration code. It delegates to the
existing research pipeline — runner, backtest, validation, gate, experiments
— and never re-implements pipeline logic.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest import (
    BacktestConfig,
    VectorBTResearchEngine,
    run_walk_forward,
)
from backtest.validation import validation_consistency
from portfolio import EqualWeightConstructor
from research.contracts import CostModel, Experiment, MarketData, ResearchInputError
from research.experiments import ExperimentManager
from research.gate import GateDecision, ResearchGate, generate_placebo_results
from research.runner import run_strategy
from research.strategies import Strategy, strategy_from_name

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------

STRATEGY_CATALOGUE: dict[str, dict[str, Any]] = {
    "momentum": {
        "label": "Momentum",
        "description": "Long-only trailing momentum. Buys assets with positive trailing returns.",
        "parameters": {
            "lookback": {
                "type": "int",
                "default": 63,
                "min": 5,
                "max": 500,
                "label": "Lookback (days)",
                "help": "Trailing window for momentum calculation",
            },
            "threshold": {
                "type": "float",
                "default": 0.0,
                "min": -1.0,
                "max": 1.0,
                "step": 0.05,
                "label": "Signal threshold",
                "help": "Minimum momentum value to generate a signal",
            },
        },
    },
    "crossover": {
        "label": "Moving Average Crossover",
        "description": "Long-only MA crossover. Signal is active when fast MA is above slow MA.",
        "parameters": {
            "fast_window": {
                "type": "int",
                "default": 20,
                "min": 3,
                "max": 200,
                "label": "Fast window",
                "help": "Short moving average span (days)",
            },
            "slow_window": {
                "type": "int",
                "default": 50,
                "min": 10,
                "max": 500,
                "label": "Slow window",
                "help": "Long moving average span (days)",
            },
            "method": {
                "type": "choice",
                "default": "sma",
                "options": ["sma", "ema"],
                "label": "MA type",
                "help": "Simple or exponential moving average",
            },
        },
    },
    "mean_reversion": {
        "label": "Mean Reversion (Z-Score)",
        "description": "Long-only when price drops below a z-score threshold (buy the dip).",
        "parameters": {
            "window": {
                "type": "int",
                "default": 20,
                "min": 5,
                "max": 200,
                "label": "Lookback window",
                "help": "Rolling window for z-score calculation",
            },
            "entry_zscore": {
                "type": "float",
                "default": -1.0,
                "min": -3.0,
                "max": -0.1,
                "step": 0.1,
                "label": "Entry z-score",
                "help": "Signal triggers when z-score falls below this (must be negative)",
            },
            "bollinger": {
                "type": "bool",
                "default": False,
                "label": "Use Bollinger deviation",
                "help": "Use Bollinger band deviation instead of plain z-score",
            },
        },
    },
}


def list_strategies() -> dict[str, dict[str, Any]]:
    """Return the strategy catalogue with metadata for the UI."""
    return STRATEGY_CATALOGUE


# ---------------------------------------------------------------------------
# Data status
# ---------------------------------------------------------------------------


def get_data_status(
    prices_path: Path | str = Path("data/clean/prices.parquet"),
) -> dict[str, Any]:
    """Check what data is available without loading it into memory."""
    prices_path = Path(prices_path)
    status: dict[str, Any] = {
        "prices_file": str(prices_path),
        "prices_exists": prices_path.is_file(),
        "prices_size_mb": (
            round(prices_path.stat().st_size / 1_048_576, 2)
            if prices_path.is_file()
            else None
        ),
        "universe_files": {},
    }
    for name in ("nifty50", "nifty100", "nifty500"):
        path = Path(f"data/universe/{name}.csv")
        status["universe_files"][name] = {
            "exists": path.is_file(),
            "path": str(path),
        }
    # Try to peek at price data dimensions if available
    if prices_path.is_file():
        try:
            if prices_path.suffix == ".parquet":
                pf = pd.read_parquet(prices_path)
            else:
                pf = pd.read_csv(prices_path)
            if {"date", "symbol", "close"}.issubset(pf.columns):
                dates = pf["date"].nunique()
                symbols = pf["symbol"].nunique()
                date_range = str(pf["date"].min()) + " to " + str(pf["date"].max())
                status["prices_info"] = {
                    "format": "long",
                    "dates": int(dates),
                    "symbols": int(symbols),
                    "date_range": date_range,
                }
            else:
                if "date" in pf.columns:
                    pf = pf.set_index("date")
                status["prices_info"] = {
                    "format": "wide",
                    "dates": len(pf),
                    "symbols": len(pf.columns),
                    "date_range": str(pf.index[0]) + " to " + str(pf.index[-1]),
                }
        except Exception as exc:
            status["prices_error"] = str(exc)
    return status


# ---------------------------------------------------------------------------
# Price data loading
# ---------------------------------------------------------------------------


def load_prices(path: Path | str) -> MarketData:
    """Load prices into MarketData, matching the CLI loader logic."""
    path = Path(path)
    if not path.is_file():
        raise ResearchInputError(f"price file does not exist: {path}")
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path)
    if {"date", "symbol", "close"}.issubset(frame.columns):
        return MarketData.from_long_frame(frame)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        frame = frame.set_index("date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="raise")
    return MarketData(close=frame)


def generate_synthetic_prices(
    n_symbols: int = 10,
    n_days: int = 756,
    seed: int = 42,
) -> MarketData:
    """Generate synthetic price data for dashboard testing.

    Uses a geometric Brownian motion model with correlated assets.
    This is for **dashboard functionality testing only** — real research
    requires real market data.
    """
    rng = np.random.RandomState(seed)
    symbols = [f"SYNTH{i:03d}" for i in range(n_symbols)]
    dates = pd.bdate_range(start="2020-01-02", periods=n_days)

    base_returns = rng.normal(0.0003, 0.015, size=(n_days, 1))
    idio = rng.normal(0, 0.02, size=(n_days, n_symbols))
    log_returns = base_returns + idio

    initial_prices = rng.uniform(100, 1000, size=n_symbols)
    cum_returns = np.exp(np.cumsum(log_returns, axis=0))
    prices = initial_prices * cum_returns

    close = pd.DataFrame(prices, index=dates, columns=symbols)
    return MarketData(close=close)


# ---------------------------------------------------------------------------
# Experiment execution
# ---------------------------------------------------------------------------


@dataclass
class ExperimentResult:
    """Complete result from one research experiment."""

    run_id: str
    strategy: str
    parameters: dict[str, Any]
    status: str  # "accepted" or "rejected"
    verdict: str  # PASS, FAIL, FRAGILE, INSUFFICIENT_EVIDENCE
    score: float
    metrics: dict[str, Any]
    gate_checks: list[dict[str, Any]]
    validation: dict[str, Any]
    benchmarks: dict[str, dict[str, Any]]
    consistency: dict[str, Any]
    rejection_reason: str | None
    started_at: str
    ended_at: str
    equity_curve_data: list[dict[str, Any]] | None = None
    drawdown_data: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "parameters": self.parameters,
            "status": self.status,
            "verdict": self.verdict,
            "score": self.score,
            "metrics": self.metrics,
            "gate_checks": self.gate_checks,
            "validation": self.validation,
            "benchmarks": self.benchmarks,
            "consistency": self.consistency,
            "rejection_reason": self.rejection_reason,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "equity_curve_data": self.equity_curve_data,
            "drawdown_data": self.drawdown_data,
        }


def run_experiment(
    strategy_name: str,
    parameters: dict[str, Any] | None = None,
    *,
    prices_path: str | Path | None = None,
    use_synthetic: bool = False,
    train_size: int = 252,
    test_size: int = 63,
    step_size: int | None = None,
    expanding: bool = False,
    placebo_samples: int = 50,
    seed: int = 42,
    tracking_dir: str | Path = Path("reports/generated/experiments"),
) -> ExperimentResult:
    """Execute the full research pipeline for one experiment.

    This is the main entry point called by the dashboard when the user
    clicks "Run Research". It wires together the existing research modules:

        strategy -> data -> backtest -> costs -> metrics ->
        walk-forward -> diagnostics -> gate -> persistence
    """
    started_at = datetime.now(UTC)
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    parameters = parameters or {}

    # 1. Load data
    if use_synthetic:
        data = generate_synthetic_prices(seed=seed)
    elif prices_path:
        data = load_prices(Path(prices_path))
    else:
        data = load_prices(Path("data/clean/prices.parquet"))

    # 2. Create strategy
    strategy = strategy_from_name(strategy_name, parameters)

    # 3. Run backtest + benchmarks
    run = run_strategy(strategy, data, random_seed=seed)

    # 4. Walk-forward validation
    engine = VectorBTResearchEngine(BacktestConfig())
    wf_result = run_walk_forward(
        strategy,
        data,
        EqualWeightConstructor(),
        engine,
        train_size=train_size,
        test_size=test_size,
        step_size=step_size,
        expanding=expanding,
    )

    # 5. Consistency check
    consistency = validation_consistency(wf_result)

    # 6. Placebo family
    placebo = generate_placebo_results(
        data.close, engine=engine, samples=placebo_samples, seed=seed
    )

    # 7. Research gate
    gate = ResearchGate(random_seed=seed)
    decision = gate.evaluate(
        run.result,
        benchmarks=run.benchmarks,
        validation=wf_result,
        placebo_results=placebo,
        rebalance_frequency="M",
        validation_method="walk_forward",
    )

    # 8. Build benchmark metrics dict
    benchmark_metrics = {}
    for name, bench_result in run.benchmarks.items():
        benchmark_metrics[name] = bench_result.metrics.to_dict()

    # 9. Extract gate check details
    gate_checks = [check.to_dict() for check in decision.checks]

    # 10. Validation payload
    validation_payload = wf_result.to_dict()

    # 11. Build failure reason from gate checks
    rejection_reason = None
    if decision.verdict != "PASS":
        failed_checks = [c for c in decision.checks if c.status == "fail"]
        if failed_checks:
            rejection_reason = "; ".join(
                f"{c.name}: {c.message}" for c in failed_checks
            )
        else:
            warned_checks = [c for c in decision.checks if c.status == "warn"]
            rejection_reason = (
                f"Verdict: {decision.verdict}; "
                + "; ".join(f"{c.name}: {c.message}" for c in warned_checks)
            )

    # 12. Equity curve data for charts
    from backtest.metrics import drawdown as compute_drawdown

    equity_series = run.result.equity_curve
    equity_curve_data = [
        {"date": idx.isoformat(), "value": float(val)}
        for idx, val in equity_series.items()
    ]
    dd_series = compute_drawdown(equity_series)
    drawdown_data = [
        {"date": idx.isoformat(), "value": float(val)}
        for idx, val in dd_series.items()
    ]

    # 13. Metrics
    metrics = run.result.metrics.to_dict()

    # 14. Persist experiment
    ended_at = datetime.now(UTC)
    experiment = Experiment(
        hypothesis_id=run_id,
        strategy=strategy.name,
        parameters=dict(strategy.parameters),
        factor_set=(strategy.name,),
        universe=f"symbols:{','.join(data.close.columns[:20])}",
    )

    try:
        manager = ExperimentManager(tracking_dir=Path(tracking_dir))
        manager.log_experiment(
            experiment,
            result=run.result,
            validation=validation_payload,
            benchmarks=run.benchmarks,
            rejected=(decision.verdict != "PASS"),
            reason=rejection_reason,
            validation_method="walk_forward",
            random_seed=seed,
            gate_result=decision,
        )
    except Exception as exc:
        logger.warning("experiment_persistence_failed: %s", exc)

    return ExperimentResult(
        run_id=run_id,
        strategy=strategy.name,
        parameters=dict(strategy.parameters),
        status="rejected" if decision.verdict != "PASS" else "accepted",
        verdict=decision.verdict,
        score=decision.score,
        metrics=metrics,
        gate_checks=gate_checks,
        validation=validation_payload,
        benchmarks=benchmark_metrics,
        consistency=consistency,
        rejection_reason=rejection_reason,
        started_at=started_at.isoformat(),
        ended_at=ended_at.isoformat(),
        equity_curve_data=equity_curve_data,
        drawdown_data=drawdown_data,
    )


# ---------------------------------------------------------------------------
# Experiment history
# ---------------------------------------------------------------------------


def list_experiments(
    tracking_dir: str | Path = Path("reports/generated/experiments"),
) -> list[dict[str, Any]]:
    """Return all persisted experiment records."""
    tracking_dir = Path(tracking_dir)
    history_path = tracking_dir / "experiments.jsonl"
    if not history_path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                records.append(payload)
        except ValueError:
            continue
    return records


def get_experiment(
    run_id: str,
    tracking_dir: str | Path = Path("reports/generated/experiments"),
) -> dict[str, Any] | None:
    """Load a single experiment by run_id or hypothesis_id.

    When running without MLflow the ``run_id`` field in the JSONL record
    is always ``"local"``.  The caller-supplied run identifier is stored
    in ``hypothesis_id`` instead, so this function matches against both.
    """
    for record in list_experiments(tracking_dir):
        if record.get("run_id") == run_id or record.get("hypothesis_id") == run_id:
            return record
    return None
