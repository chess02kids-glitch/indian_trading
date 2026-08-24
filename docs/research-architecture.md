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
