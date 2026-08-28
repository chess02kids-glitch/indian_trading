"""Predefined Candidate Strategy Set and Automated Research Protocol.

Implements the 8-strategy research candidate set (S01 through S08) and the
standard evaluation protocol:

    Historical Data
          ↓
    Candidate Strategy (S01-S08)
          ↓
    Backtest (Transaction Costs + Slippage)
          ↓
    Purged & Embargoed Walk-Forward
          ↓
    CPCV (Combinatorial Purged Cross-Validation)
          ↓
    Cost Stress Testing (1x, 2x, 3x)
          ↓
    Deflated Sharpe (Multiple-Testing Correction)
          ↓
    Placebo Family Dominance
          ↓
    Research Gate Verdict (PASS / FRAGILE / FAIL / INSUFFICIENT_EVIDENCE)
          ↓
    Hypothesis Ledger & Rejection Insights Persistence

The primary objective is not solely to find high backtest Sharpes, but to
verify whether the research gate and protocol can correctly kill seductive
strategies that fail out-of-sample, suffer from heavy transaction cost drag,
or fail multiple-testing hurdles.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest import (
    BacktestConfig,
    VectorBTResearchEngine,
    run_combinatorial_purged_cv,
    run_walk_forward,
)
from backtest.engine import BacktestResult
from backtest.validation import (
    CrossValidationResult,
    DeflatedSharpeResult,
    WalkForwardResult,
    bootstrap_metric_intervals,
    deflated_sharpe_from_returns,
    validation_consistency,
)
from portfolio.construction import EqualWeightConstructor
from research.contracts import Experiment, MarketData, ResearchInputError, Strategy
from research.experiments import ExperimentManager
from research.gate import (
    GateDecision,
    GateVerdict,
    ResearchGate,
    ResearchGateConfig,
    generate_placebo_results,
)
from research.ledger import HypothesisLedger, HypothesisRecord
from research.runner import run_strategy
from research.strategies import (
    CrossSectionalMomentumStrategy,
    DonchianTrendStrategy,
    GapFadeStrategy,
    LowVolatilityStrategy,
    OrbStrategy,
    PairsTradingStrategy,
    RsiMeanReversionStrategy,
    ValueQualityStrategy,
)

__all__ = [
    "CANDIDATE_STRATEGY_SPECS",
    "CandidateEvaluationResult",
    "CandidateSetReport",
    "CandidateStrategySpec",
    "evaluate_candidate_set",
    "get_candidate_strategy",
    "run_candidate_protocol",
]


@dataclass(frozen=True, slots=True)
class CandidateStrategySpec:
    """Specification and hypothesis details for one candidate strategy."""

    candidate_id: str
    name: str
    title: str
    description: str
    academic_basis: str
    priority: int
    default_parameters: Mapping[str, Any]
    factory: Any


CANDIDATE_STRATEGY_SPECS: tuple[CandidateStrategySpec, ...] = (
    CandidateStrategySpec(
        candidate_id="S01",
        name="cross_sectional_momentum",
        title="Cross-Sectional Momentum",
        description="Ranks universe by trailing momentum (3M/6M/12M); longs top quantile, avoids bottom losers.",
        academic_basis="Jegadeesh & Titman (1993); widespread academic replication across equity markets.",
        priority=1,
        default_parameters={"lookback": 63, "quantile": 0.20, "multi_horizon": False},
        factory=lambda params: CrossSectionalMomentumStrategy(
            lookback=int(params.get("lookback", 63)),
            quantile=float(params.get("quantile", 0.20)),
            multi_horizon=bool(params.get("multi_horizon", False)),
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S02",
        name="donchian_trend",
        title="Donchian / Trend Following",
        description="Mechanical 20/10 day channel breakout with zero lookahead state persistence.",
        academic_basis="Richard Dennis & William Eckhardt Turtle rules (1983); Mount Lucas Management trend benchmark.",
        priority=2,
        default_parameters={
            "entry_window": 20,
            "exit_window": 10,
            "volatility_weighted": False,
        },
        factory=lambda params: DonchianTrendStrategy(
            entry_window=int(params.get("entry_window", 20)),
            exit_window=int(params.get("exit_window", 10)),
            volatility_weighted=bool(params.get("volatility_weighted", False)),
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S03",
        name="pairs_trading",
        title="Pairs Trading / Stat-Arb",
        description="Spread z-score statistical arbitrage; enters on 2.0 std divergence, exits at mean reversion.",
        academic_basis="Gatev, Goetzmann & Rouwenhorst (2006); cointegration pairs trading.",
        priority=3,
        default_parameters={
            "window": 60,
            "entry_zscore": 2.0,
            "exit_zscore": 0.5,
            "stop_zscore": 3.5,
        },
        factory=lambda params: PairsTradingStrategy(
            window=int(params.get("window", 60)),
            entry_zscore=float(params.get("entry_zscore", 2.0)),
            exit_zscore=float(params.get("exit_zscore", 0.5)),
            stop_zscore=float(params.get("stop_zscore", 3.5)),
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S04",
        name="rsi_mean_reversion",
        title="RSI Mean Reversion",
        description="Buys oversold RSI (<30) and exits on overbought (>70) with optional regime trend filter.",
        academic_basis="Connors RSI-2 & Wilder relative strength mean reversion.",
        priority=4,
        default_parameters={
            "rsi_window": 14,
            "oversold": 30.0,
            "overbought": 70.0,
            "trend_filter_window": 200,
        },
        factory=lambda params: RsiMeanReversionStrategy(
            rsi_window=int(params.get("rsi_window", 14)),
            oversold=float(params.get("oversold", 30.0)),
            overbought=float(params.get("overbought", 70.0)),
            trend_filter_window=int(params["trend_filter_window"])
            if params.get("trend_filter_window")
            else None,
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S05",
        name="orb",
        title="Opening Range Breakout (ORB)",
        description="Intraday breakout beyond opening range hurdle with realistic slippage and transaction costs.",
        academic_basis="Crabel (1990) Day Trading with Short Term Price Patterns.",
        priority=5,
        default_parameters={"range_factor": 0.5, "atr_window": 14},
        factory=lambda params: OrbStrategy(
            range_factor=float(params.get("range_factor", 0.5)),
            atr_window=int(params.get("atr_window", 14)),
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S06",
        name="gap_fade",
        title="Gap Fade",
        description="Identifies moderate overnight gap-downs (-0.5% to -3.5%) and bets on intraday mean-reversion bounce.",
        academic_basis="Overnight return anomalies & opening gap mean-reversion in index equities.",
        priority=6,
        default_parameters={"min_gap_pct": -0.005, "max_gap_pct": -0.035},
        factory=lambda params: GapFadeStrategy(
            min_gap_pct=float(params.get("min_gap_pct", -0.005)),
            max_gap_pct=float(params.get("max_gap_pct", -0.035)),
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S07",
        name="low_volatility",
        title="Low-Volatility Factor",
        description="Ranks equities by inverse realized volatility; selects the top quantile of lowest-volatility names.",
        academic_basis="Blitz & van Vliet (2007) The Volatility Anomaly; Haugen & Baker (1991).",
        priority=7,
        default_parameters={"vol_window": 63, "quantile": 0.25},
        factory=lambda params: LowVolatilityStrategy(
            vol_window=int(params.get("vol_window", 63)),
            quantile=float(params.get("quantile", 0.25)),
        ),
    ),
    CandidateStrategySpec(
        candidate_id="S08",
        name="value_quality",
        title="Value & Quality Factor",
        description="Composite screen selecting high return-on-equity, low debt/equity leverage, and value tilt.",
        academic_basis="Fama & French (2015) 5-factor model; Novy-Marx (2013) Quality Investing.",
        priority=8,
        default_parameters={"quality_quantile": 0.5, "value_quantile": 0.5},
        factory=lambda params: ValueQualityStrategy(
            quality_quantile=float(params.get("quality_quantile", 0.5)),
            value_quantile=float(params.get("value_quantile", 0.5)),
        ),
    ),
)


def get_candidate_strategy(
    candidate_id_or_name: str, parameters: Mapping[str, Any] | None = None
) -> tuple[CandidateStrategySpec, Strategy]:
    """Resolve a candidate specification and instantiate its strategy."""
    query = candidate_id_or_name.strip().lower().replace("-", "_")
    for spec in CANDIDATE_STRATEGY_SPECS:
        if (
            spec.candidate_id.lower() == query
            or spec.name.lower() == query
            or f"{spec.candidate_id.lower()}_{spec.name.lower()}" == query
        ):
            merged_params = {**spec.default_parameters, **(parameters or {})}
            return spec, spec.factory(merged_params)
    raise ResearchInputError(f"unknown candidate strategy: {candidate_id_or_name}")


@dataclass(frozen=True, slots=True)
class CandidateEvaluationResult:
    """Complete auditable result from evaluating one candidate against the research gate."""

    candidate_id: str
    name: str
    title: str
    strategy: Strategy
    backtest_result: BacktestResult
    walk_forward_result: WalkForwardResult
    cpcv_result: CrossValidationResult | None
    gate_decision: GateDecision
    deflated_sharpe: DeflatedSharpeResult
    cost_stress_sharpe: Mapping[str, float]
    consistency: Mapping[str, Any]
    rejection_reasons: tuple[str, ...]
    rejection_insights: Mapping[str, Any]
    hypothesis_record: HypothesisRecord

    def to_dict(self) -> dict[str, Any]:
        """Serialize as JSON-compatible dictionary."""
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "title": self.title,
            "strategy": self.strategy.name,
            "parameters": dict(self.strategy.parameters),
            "verdict": self.gate_decision.verdict,
            "score": self.gate_decision.score,
            "metrics": self.backtest_result.metrics.to_dict(),
            "deflated_sharpe": self.deflated_sharpe.to_dict(),
            "cost_stress_sharpe": dict(self.cost_stress_sharpe),
            "consistency": dict(self.consistency),
            "rejection_reasons": list(self.rejection_reasons),
            "rejection_insights": dict(self.rejection_insights),
            "hypothesis_id": self.hypothesis_record.hypothesis_id,
        }


@dataclass(frozen=True, slots=True)
class CandidateSetReport:
    """Aggregated report across the candidate strategy set."""

    evaluations: tuple[CandidateEvaluationResult, ...]
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> tuple[CandidateEvaluationResult, ...]:
        return tuple(
            e
            for e in self.evaluations
            if e.gate_decision.verdict == GateVerdict.PASS.value
        )

    @property
    def fragile(self) -> tuple[CandidateEvaluationResult, ...]:
        return tuple(
            e
            for e in self.evaluations
            if e.gate_decision.verdict == GateVerdict.FRAGILE.value
        )

    @property
    def rejected(self) -> tuple[CandidateEvaluationResult, ...]:
        return tuple(
            e
            for e in self.evaluations
            if e.gate_decision.verdict == GateVerdict.FAIL.value
        )

    def to_dataframe(self) -> pd.DataFrame:
        """Render a comparative overview table."""
        rows = []
        for e in self.evaluations:
            m = e.backtest_result.metrics
            rows.append(
                {
                    "Candidate": e.candidate_id,
                    "Strategy": e.title,
                    "Verdict": e.gate_decision.verdict,
                    "Score": round(e.gate_decision.score, 1),
                    "Sharpe (Net)": round(float(m.sharpe), 2),
                    "CAGR": f"{float(m.annualized_return):.1%}",
                    "Max DD": f"{float(m.max_drawdown):.1%}",
                    "Turnover": round(float(m.turnover), 1),
                    "DSR Prob": f"{e.deflated_sharpe.probability:.1%}",
                    "Fold Consist": f"{e.consistency.get('positive_fold_fraction', 0.0):.0%}",
                    "Primary Reason / Insight": e.rejection_reasons[0]
                    if e.rejection_reasons
                    else "Passed all gate requirements",
                }
            )
        return pd.DataFrame(rows)

    def to_markdown(self) -> str:
        """Render human-readable Markdown summary report."""
        lines = [
            "# Candidate Strategy Set — Protocol Research Report",
            "",
            f"**Evaluated At**: `{self.generated_at.isoformat()}`  ",
            f"**Candidates Tested**: `{len(self.evaluations)}` | **Passed**: `{len(self.passed)}` | **Fragile**: `{len(self.fragile)}` | **Rejected**: `{len(self.rejected)}`",
            "",
            "## Summary Matrix",
            "",
            "| ID | Strategy | Verdict | Score | Sharpe | CAGR | Max DD | Turnover | DSR Prob | Fold Consist | Key Gate Insight |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for e in self.evaluations:
            m = e.backtest_result.metrics
            reason = (
                e.rejection_reasons[0].replace("|", "\\|")
                if e.rejection_reasons
                else "Passed all gate checks"
            )
            lines.append(
                f"| **{e.candidate_id}** | {e.title} | `{e.gate_decision.verdict}` | {e.gate_decision.score:.1f} | "
                f"{float(m.sharpe):.2f} | {float(m.annualized_return):.1%} | {float(m.max_drawdown):.1%} | "
                f"{float(m.turnover):.1f}x | {e.deflated_sharpe.probability:.1%} | "
                f"{e.consistency.get('positive_fold_fraction', 0.0):.0%} | {reason} |"
            )

        lines.extend(["", "## Strategy Failure Analysis & Rejection Taxonomy", ""])
        for e in self.evaluations:
            status_symbol = (
                "✅"
                if e.gate_decision.verdict == "PASS"
                else ("⚠️" if e.gate_decision.verdict == "FRAGILE" else "❌")
            )
            lines.append(
                f"### {status_symbol} {e.candidate_id}: {e.title} (`{e.gate_decision.verdict}`)"
            )
            lines.append(
                f"- **Hypothesis Record**: `{e.hypothesis_record.hypothesis_id}`"
            )
            lines.append(f"- **Academic Basis**: {e.hypothesis_record.hypothesis}")
            lines.append(
                f"- **Cost Stress Sharpe**: 1x: `{e.cost_stress_sharpe.get('1x', 0):.2f}`, 2x: `{e.cost_stress_sharpe.get('2x', 0):.2f}`, 3x: `{e.cost_stress_sharpe.get('3x', 0):.2f}`"
            )
            if e.rejection_reasons:
                lines.append("- **Gate Rejection Reasons**:")
                for r in e.rejection_reasons:
                    lines.append(f"  - {r}")
            else:
                lines.append(
                    "- **Status**: Meets all statistical confidence, cost robustness, and fold consistency hurdles."
                )
            lines.append("")

        return "\n".join(lines) + "\n"

    def write(self, output_dir: Path | str) -> tuple[Path, Path]:
        """Write report artifacts (JSON + Markdown) to disk."""
        directory = Path(output_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        md_path = directory / "candidate_set_report.md"
        json_path = directory / "candidate_set_report.json"

        md_path.write_text(self.to_markdown(), encoding="utf-8")
        json_payload = {
            "generated_at": self.generated_at.isoformat(),
            "total_candidates": len(self.evaluations),
            "passed_count": len(self.passed),
            "fragile_count": len(self.fragile),
            "rejected_count": len(self.rejected),
            "candidates": [e.to_dict() for e in self.evaluations],
        }
        json_path.write_text(
            json.dumps(json_payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return md_path, json_path


def run_candidate_protocol(
    spec: CandidateStrategySpec,
    data: MarketData,
    *,
    parameters: Mapping[str, Any] | None = None,
    total_trials: int = 8,
    train_size: int = 252,
    test_size: int = 63,
    placebo_samples: int = 50,
    seed: int = 42,
    ledger: HypothesisLedger | None = None,
    experiment_manager: ExperimentManager | None = None,
    run_cpcv: bool = False,
) -> CandidateEvaluationResult:
    """Evaluate one candidate strategy through the full research protocol."""
    merged_params = {**spec.default_parameters, **(parameters or {})}
    strategy = spec.factory(merged_params)

    # 1. Standard Backtest and Benchmarks
    engine = VectorBTResearchEngine(BacktestConfig())
    run = run_strategy(strategy, data, engine=engine, random_seed=seed)
    returns = run.result.returns

    # 2. Walk-forward Validation
    constructor = EqualWeightConstructor()
    wf_result = run_walk_forward(
        strategy,
        data,
        constructor,
        engine,
        train_size=train_size,
        test_size=test_size,
        purge=5,
        embargo=5,
    )
    consistency = validation_consistency(wf_result)

    # 3. CPCV Validation (Optional)
    cpcv_result = None
    if run_cpcv:
        try:
            cpcv_result = run_combinatorial_purged_cv(
                strategy,
                data,
                constructor,
                engine,
                n_groups=6,
                n_test_groups=2,
                purge=5,
                embargo=5,
            )
        except Exception:
            cpcv_result = None

    # 4. Placebo Portfolios
    placebo = generate_placebo_results(
        data.close, engine=engine, samples=placebo_samples, seed=seed
    )

    # 5. Deflated Sharpe Ratio with Multi-Testing Correction
    dsr = deflated_sharpe_from_returns(returns, trials=total_trials)

    # 6. Bootstrap Intervals
    bootstrap_metric_intervals(
        returns,
        turnover=run.result.trades["turnover"],
        samples=500,
        seed=seed,
    )

    # 7. Cost Stress Testing (1x, 2x, 3x)
    gross_returns = returns + run.result.trades["total_cost"].astype(float)
    cost_series = run.result.trades["total_cost"].astype(float)
    cost_stress: dict[str, float] = {
        "1x": float(run.result.metrics.sharpe),
        "2x": float(
            (gross_returns - 2.0 * cost_series).mean()
            / (returns.std(ddof=1) if returns.std(ddof=1) > 0 else 1.0)
            * np.sqrt(252)
        ),
        "3x": float(
            (gross_returns - 3.0 * cost_series).mean()
            / (returns.std(ddof=1) if returns.std(ddof=1) > 0 else 1.0)
            * np.sqrt(252)
        ),
    }

    # 8. Research Gate Decision
    gate_config = ResearchGateConfig(tested_variants=total_trials)
    gate = ResearchGate(config=gate_config, random_seed=seed)
    decision = gate.evaluate(
        run.result,
        benchmarks=run.benchmarks,
        validation=wf_result,
        placebo_results=placebo,
        rebalance_frequency="M",
        validation_method="walk_forward",
    )

    # 9. Extract Rejection Reasons and Insights
    rejection_reasons = tuple(
        f"[{check.status}] {check.name}: {check.message}"
        for check in decision.checks
        if check.status in ("fail", "warn")
    )
    rejection_insights = {
        "failed_checks": [c.name for c in decision.checks if c.status == "fail"],
        "warned_checks": [c.name for c in decision.checks if c.status == "warn"],
        "cost_fragility": cost_stress["2x"] <= 0.0,
        "multiple_testing_risk": dsr.probability < 0.95,
        "fold_dispersion": consistency.get("fold_sharpe_std", 0.0),
    }

    # 10. Record in Hypothesis Ledger
    if ledger is None:
        ledger = HypothesisLedger(
            "reports/generated/experiments/hypothesis_ledger.jsonl"
        )

    hyp_status = (
        "accepted" if decision.verdict == GateVerdict.PASS.value else "rejected"
    )
    primary_reason = (
        rejection_reasons[0]
        if rejection_reasons
        else "Passed all research gate criteria"
    )

    hyp_record = ledger.record(
        status=hyp_status,
        hypothesis=f"{spec.candidate_id}: {spec.title} — {spec.academic_basis}",
        strategy=strategy.name,
        parameters=dict(strategy.parameters),
        metrics=run.result.metrics.to_dict(),
        reason=primary_reason,
        gate_result=decision.to_dict(),
    )

    # 11. Record in Experiment Manager if provided
    if experiment_manager is not None:
        try:
            exp = Experiment(
                hypothesis_id=hyp_record.hypothesis_id,
                strategy=strategy.name,
                parameters=dict(strategy.parameters),
                factor_set=(strategy.name,),
                universe=f"symbols:{','.join(data.close.columns[:20])}",
            )
            experiment_manager.log_experiment(
                exp,
                result=run.result,
                validation=wf_result.to_dict(),
                benchmarks=run.benchmarks,
                rejected=(decision.verdict != GateVerdict.PASS.value),
                reason=primary_reason,
                gate_result=decision,
                random_seed=seed,
            )
        except Exception:
            pass

    return CandidateEvaluationResult(
        candidate_id=spec.candidate_id,
        name=spec.name,
        title=spec.title,
        strategy=strategy,
        backtest_result=run.result,
        walk_forward_result=wf_result,
        cpcv_result=cpcv_result,
        gate_decision=decision,
        deflated_sharpe=dsr,
        cost_stress_sharpe=cost_stress,
        consistency=consistency,
        rejection_reasons=rejection_reasons,
        rejection_insights=rejection_insights,
        hypothesis_record=hyp_record,
    )


def evaluate_candidate_set(
    data: MarketData,
    *,
    candidate_specs: Sequence[CandidateStrategySpec] | None = None,
    parameters_map: Mapping[str, Mapping[str, Any]] | None = None,
    train_size: int = 252,
    test_size: int = 63,
    placebo_samples: int = 50,
    seed: int = 42,
    ledger_path: Path | str = "reports/generated/experiments/hypothesis_ledger.jsonl",
    tracking_dir: Path | str = "reports/generated/experiments",
    run_cpcv: bool = False,
) -> CandidateSetReport:
    """Run all strategy candidates in the candidate set through the research protocol."""
    specs = candidate_specs or CANDIDATE_STRATEGY_SPECS
    params_map = parameters_map or {}
    total_trials = len(specs)

    ledger = HypothesisLedger(ledger_path)
    manager = ExperimentManager(tracking_dir=tracking_dir)

    results: list[CandidateEvaluationResult] = []
    for spec in specs:
        custom_params = params_map.get(spec.candidate_id) or params_map.get(spec.name)
        eval_result = run_candidate_protocol(
            spec,
            data,
            parameters=custom_params,
            total_trials=total_trials,
            train_size=train_size,
            test_size=test_size,
            placebo_samples=placebo_samples,
            seed=seed,
            ledger=ledger,
            experiment_manager=manager,
            run_cpcv=run_cpcv,
        )
        results.append(eval_result)

    return CandidateSetReport(evaluations=tuple(results))
