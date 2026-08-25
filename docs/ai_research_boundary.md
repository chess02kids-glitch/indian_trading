# AI research boundary

The AI is a **hypothesis proposer**. It is not a trader.

```
AI
 ↓
ResearchHypothesis (Pydantic, extra=forbid)
 ↓
allowed strategy registry
 ↓
allowed parameter bounds
 ↓
novelty + campaign budget
 ↓
deterministic research engine
 ↓
validation + research gate
 ↓
ledger
 ↓
structured result back to AI
```

## Allowed

* `registered_strategy_id` / `strategy_family` from `research.registry`
* `feature_id` from the published feature list
* scalar parameters inside `ALLOWED_PARAMETER_BOUNDS`

## Forbidden

* `exec` / `eval` / generated Python
* fields named `code`, `python`, `exec`, `eval`, `source`, `module`
* imports of `broker`, `execution`, `risk_kill`, `orchestration`, `auth`
* choosing the cost model after seeing results
* reading the locked holdout to propose the next idea (context builder
  exposes ledger outcomes, not holdout return series)

`research.ai_boundary.submit_hypothesis` instantiates the registered
strategy to prove constructibility and **does not** run a backtest.

`build_research_context` supplies previous hypotheses, family counts,
failure reasons, budget, campaign snapshot, and available features so
the AI cannot honestly claim ignorance of prior 3M/6M/12M momentum
trials.

Near-duplicates become ledger status `duplicate` with reason
`REJECTED_DUPLICATE`.
