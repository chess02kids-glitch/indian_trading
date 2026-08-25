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

Campaigns (`ResearchCampaignStore`), the strategy registry (`instantiate`,
`BENCHMARK_ZOO`), hypothesis schema (`ResearchHypothesis`), and the AI
boundary (`submit_hypothesis`) live in this package. They never import
broker, execution, or risk modules.

All factors expose metadata and parameters. Backtest execution shifts target
weights by one observation, so factor values calculated at a close cannot trade
on that same close.

See [`docs/research-architecture.md`](../docs/research-architecture.md),
[`docs/factors.md`](../docs/factors.md), and
[`docs/data-foundation.md`](../docs/data-foundation.md).
