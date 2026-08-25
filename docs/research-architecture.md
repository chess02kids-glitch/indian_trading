# Research architecture

## Boundaries

The research platform sits above the data foundation:

```text
Parquet / DuckDB -> MarketData -> Factors -> Signals -> Allocations
                                      -> VectorBTResearchEngine
                                      -> Metrics / Validation / Reports
                                      -> MLflow experiment history
```

Research code is intentionally downstream of ingestion and upstream of any
future execution work. Nothing in this package creates orders or imports broker,
authentication, execution, risk-kill, or agent modules.

## Contracts

`research.contracts` defines the reusable interfaces:

- `MarketData` contains aligned close and optional OHLCV panels and can be
  restricted to a configured symbol set with `select()`.
- `Factor` computes an aligned factor panel and exposes `FactorMetadata`.
- `Signal` carries strategy values and metadata.
- `Strategy` generates deterministic signals.
- `PortfolioConstructor` converts signals into research-only target weights.
- `CostModel` centralizes transaction-cost and slippage assumptions.
- `Experiment` identifies a hypothesis, strategy, parameters, factor set, and
  universe with a stable hash.

## Historical universe dataset

`data.universe` is the single source of truth for *historical* index
membership (Nifty 50 / 100 / 500). Each constituent records a validity window
(`valid_from` / `valid_to`); removed/delisted names are retained for
survivorship-bias protection. `data.universe.UniverseDataset.members_at`
resolves point-in-time membership and `validate_period` refuses backtest
periods the dataset does not cover. `research.universe.build_universe_from_dataset`
turns a dataset into a `Universe`, and `ensure_universe_period_covers`
(or `research.runner.run_strategy`) refuses invalid universe dates at the
backtest boundary.

## Data-quality and calendar validation

`data.quality` rejects invalid OHLCV rows (never interpolating) and reports
missing candles, duplicates, staleness, and — via `TradingCalendar` /
`detect_off_calendar_candles` — candles on non-trading days. The clean dataset
pipeline (`data.dataset.CleanDataCatalog`) writes validated long-form Parquet
with a metadata sidecar, registers a DuckDB view, and exposes a dataset
fingerprint for experiment provenance.

## Portfolio reporting

`research.reporting.generate_periodic_reports` produces daily/weekly/monthly
portfolio reports (exposure, turnover, drawdown, factor exposure) and
`research.runner.run_strategy` rejects backtests over dates a historical
universe does not cover.

The runner composes those contracts without requiring notebooks:

```python
from research import MarketData, MomentumStrategy, run_strategy

research_run = run_strategy(MomentumStrategy(lookback=63), MarketData(close=prices))
comparison = research_run.comparison()
```

## Reproducibility

Every factor declares its family, version, and parameters. Universe snapshots
carry a name, optional `as_of` date, and metadata. Random benchmarks require an
explicit seed. Experiment records include a Git commit hash, parameters, factor
set, universe, timestamps, metrics, validation output, and benchmark output.

## Look-ahead policy

Factors use trailing observations. Portfolio constructors use prior realized
volatility. The backtest engine applies a target allocation to the following
observation and charges turnover at the rebalance observation. Walk-forward
validation keeps test windows temporally after training windows and supports an
embargo gap.

## Research-only portfolio outputs

Allocation objects are DataFrames indexed by date and labelled by symbol. They
are not order instructions. Execution remains outside the sprint and all
existing risk and execution invariants remain unchanged.

## v0.3 statistical validation and research gate

The research pipeline now runs:

    Historical Data -> Factor Generation -> Strategy -> VectorBT Backtest
        -> Statistical Validation (DSR, purged walk-forward, CPCV, bootstrap CIs)
        -> Research Gate (PASS / FAIL / FRAGILE / INSUFFICIENT_EVIDENCE)
        -> Factor Diagnostics -> Paper-Trading Analytics -> Long-Run Replay
        -> Research Reports -> MLflow (metadata-only)

Key modules:

- `backtest.validation` — deflated Sharpe, purged walk-forward, CPCV, and
  bootstrap confidence intervals (see `docs/validation_engine.md`).
- `research.gate` — the decision layer with explicit reasons for every
  verdict (see `docs/research_gate.md`).
- `research.diagnostics` — factor decay, sector exposure, turnover
  attribution, rank stability, volatility contributions, contribution
  breakdown.
- `research.experiments` — MLflow parameters/metrics/artifacts plus the
  local JSONL audit trail; MLflow is metadata-only.
- `research.ledger` — accepted, rejected, failed, and interrupted runs with
  duplicate-fingerprint detection; append-only and immutable.
- `research.replay` / `research.paper_analytics` — deterministic long-run
  replay and read-only paper-trading analytics.
- `dashboard/research_dashboard.py` — read-only Streamlit research views.

Everything remains deterministic; the gate is a pure function of its inputs
and research code never imports execution, broker, or risk-kill modules.
