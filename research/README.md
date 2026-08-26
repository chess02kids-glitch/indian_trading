# research

The research package provides reusable contracts, technical factors, strategy
adapters, universe selection, experiment tracking, reporting, and the research
CLI. It consumes market data and produces signal or report objects; it does not
call broker, authentication, execution, risk, or agent modules.

## Public building blocks

- `MarketData`, `Signal`, `Factor`, `Strategy`, `CostModel`, and `Experiment`
  define stable research contracts.
- `MomentumFactor`, moving averages, crossover, z-score, Bollinger deviation,
  rolling volatility, ATR, and relative-strength ranking implement deterministic
  factor families.
- `MomentumStrategy`, `CrossoverStrategy`, and `MeanReversionStrategy` plug
  into a common signal interface.
- `Universe`, `nifty_50()`, `nifty_100()`, `custom_universe()`, and
  `resolve_universe()` keep symbol selection explicit and reproducible.
- `run_strategy()` produces a strategy result and the standardized benchmark
  suite.
- `ExperimentManager` records accepted and rejected trials in MLflow and a
  local JSONL audit trail.
- `generate_report()` creates machine-readable JSON and human-readable Markdown.

All factors expose metadata and parameters. Backtest execution shifts target
weights by one observation, so factor values calculated at a close cannot trade
on that same close.

See [`docs/research-architecture.md`](../docs/research-architecture.md),
[`docs/factors.md`](../docs/factors.md), and
[`docs/data-foundation.md`](../docs/data-foundation.md).

## Research-system integrity layer (v0.8)

- `CampaignStore` / `ResearchBudget` — bounded search per campaign
  (`CMP-XXXXX`), trial reservation before evaluation,
  `RESEARCH_BUDGET_EXHAUSTED` semantics. See
  [`docs/research_campaigns.md`](../docs/research_campaigns.md).
- `StrategyRegistry` — the only way to select a strategy: registered ids
  with bounded parameters, no executable content. See
  [`docs/strategy_registry.md`](../docs/strategy_registry.md).
- `HypothesisLedger` — append-only history with lineage
  (`campaign_id`, `parent_hypothesis_id`, `strategy_family`, features,
  transformations) and every outcome status.
- `NoveltyController` — deterministic duplicate / near-duplicate control.
- `AIResearchInterface` + `ResearchContextBuilder` — validated proposal
  intake and research-history context (no results by default). See
  [`docs/ai_research_boundary.md`](../docs/ai_research_boundary.md).
- `research.dsr_accounting` — authoritative multiple-testing trial
  counts. See [`docs/dsr_accounting.md`](../docs/dsr_accounting.md).
- `research.synthetic_worlds` + `scripts/run_synthetic_worlds.py` —
  controlled worlds A–G. See [`docs/synthetic_worlds.md`](../docs/synthetic_worlds.md).
- `research.leakage` — deterministic look-ahead / survivorship /
  rank-mask-order / holdout-isolation audits. See
  [`docs/anti_overfitting.md`](../docs/anti_overfitting.md).
