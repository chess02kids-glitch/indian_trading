# AI research boundary

This document describes how an AI research agent interacts with the
repository, and — more importantly — how it **cannot**.

## The only entry point

`research/ai_research.py::AIResearchInterface.submit_proposal(payload,
campaign_id=...)` is the single door for AI-generated research. The flow
is strictly one-way:

```
AI proposal
  -> pydantic schema (extra="forbid", JSON-safe parameters)
  -> strategy registry (registered ids + declared parameter bounds only)
  -> novelty control (REJECTED_DUPLICATE / REJECTED_NEAR_DUPLICATE)
  -> campaign budget probe (RESEARCH_BUDGET_EXHAUSTED)
  -> hypothesis id allocation (running ledger record)
  -> validated hypothesis
        |
        v
  deterministic research engine (runner / zoo / backtest)
  -> gate -> ledger -> structured result
```

Outcomes: `ACCEPTED`, `REJECTED_INVALID`, `REJECTED_DUPLICATE`,
`REJECTED_NEAR_DUPLICATE`, `RESEARCH_BUDGET_EXHAUSTED`. Every rejection is
recorded in the ledger with its reason — rejections are research history.

## What the AI can choose

- a `strategy_id` from the code-defined registry
- `features` / `transformations` (declared, validated strings)
- `parameters` inside the registry's declared bounds
- a parent hypothesis id (lineage)

## What the AI can never do

- **execute code**: the schema is `extra="forbid"`; a payload field like
  `execute_code` is rejected before any code path sees it. No
  `exec`/`eval`/`pickle`/`yaml.load`/`subprocess` exists in the AI path
  (enforced by AST tests).
- **reach execution**: the AI module's import graph (verified statically)
  contains no `execution`, `broker`, `risk_kill`, `agents`, `dashboard`,
  or `backtest`/`gate` module. The interface validates and reserves; it
  never measures.
- **see holdout results by default**: `ResearchContextBuilder` excludes
  metrics and gate results unless the operator explicitly passes
  `include_results=True`. An agent cannot condition its next proposal on
  the outcome of the last experiment.
- **bypass the gate**: the interface has no gate call and no result
  path; the deterministic engine + gate are the only producers of
  verdicts.
- **change risk controls**: risk/execution modules are unreachable from
  the AI path by construction.
- **search without bound**: every accepted proposal consumes a campaign
  trial; `RESEARCH_BUDGET_EXHAUSTED` stops the search.
- **rediscover tested ideas silently**: the novelty controller rejects
  exact duplicates and caps parameter variants per family; near-duplicates
  are recorded or rejected explicitly, never silently collapsed.

## History the AI receives

`ResearchContextBuilder.build_context()` provides: campaigns with budget
state, the hypothesis history (status, strategy, family, parameters,
features, campaign, parent, reason), failed families with reasons, tested
families, and the available strategy registry. No performance numbers
unless explicitly requested.

## Tests

`tests/test_ai_research_boundary.py` enforces all of the above: static
import-graph isolation, AST scans for code-execution constructs, schema
strictness, registry bounds, duplicate rejection with lineage links,
budget exhaustion, and context-without-results.
