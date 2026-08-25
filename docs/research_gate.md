# Automated research gate

`research.gate.ResearchGate` is the decision layer between research and
paper trading. It answers one question with exactly one of four verdicts:

| Verdict | Meaning |
| --- | --- |
| `PASS` | Strong statistical evidence, beats benchmarks and placebos, costs/drawdown/turnover controlled, validation consistent. |
| `FAIL` | At least one critical check failed with an explicit reason. |
| `FRAGILE` | Nothing critically failed, but evidence is incomplete (e.g. no validation folds, no placebo family). No silent approvals. |
| `INSUFFICIENT_EVIDENCE` | Too few out-of-sample observations to draw any conclusion. |

Every decision records the eight checks that produced it, the evidence
behind each check, all confidence intervals, the benchmark comparison, and
the full reproducibility metadata (strategy/factor versions, universe,
cost model, rebalance frequency, validation method, seed, git commit,
dataset fingerprint). Decisions are never approved silently.

## Checks

1. **evidence_sufficiency** — out-of-sample observations ≥ 252 (configurable).
2. **statistical_confidence** — deflated-Sharpe probability ≥ 0.95 and the
   95% bootstrap Sharpe lower bound > 0.
3. **benchmark_competitiveness** — the candidate must beat at least 60% of
   Buy & Hold, Equal Weight, Inverse Volatility, Persistence, and Random
   on Sharpe.
4. **cost_robustness** — the share of gross return consumed by costs must
   be ≤ 50%, and a 2×-cost stress test must still produce positive Sharpe.
5. **drawdown_control** — max drawdown within −30% (configurable).
6. **turnover_control** — annualized turnover within 8× (configurable).
7. **validation_consistency** — at least 50% of walk-forward/CPCV folds show
   positive Sharpe; missing validation evidence is flagged, never approved.
8. **placebo_dominance** — the candidate must exceed the 95th percentile of
   a seeded random-placebo family; a missing placebo family is flagged.

`generate_placebo_results` builds the seeded placebo family with the same
engine, cost model, and rebalance frequency as the candidate, so the
comparison is apples-to-apples. The gate itself is a pure function of its
inputs and never touches execution or broker code.

**Evidence discipline.** `oos_returns` must be the candidate's locked
holdout returns (see `backtest.validation.run_holdout_protocol`), and the
`benchmarks` / `placebo_results` mappings must be evaluated on that same
holdout slice — never full-period results mixed with holdout evidence.
The baseline experiment wires the gate this way.

## Using the gate

```python
from research import ResearchGate, generate_placebo_results

gate = ResearchGate(random_seed=42, git_commit=commit, dataset_fingerprint=fpr)
decision = gate.evaluate(
    result,  # BacktestResult of the candidate
    benchmarks=research_run.benchmarks,
    validation=walk_forward,  # WalkForwardResult / CrossValidationResult
    placebo_results=generate_placebo_results(prices, engine=engine, samples=50),
    strategy_version="1.0",
    factor_versions={"momentum_3m": "1.0"},
    universe="nifty100",
    validation_method="walk_forward",
)
decision.to_dict()  # machine-readable record with reasons
decision.to_markdown()  # human-readable gate report
```

The gate result is logged with every experiment (`gate_result`), stored in
the ledger, written to `research_gate.json` / `research_gate_summary.json`,
and included in advanced daily/weekly/monthly research reports.
