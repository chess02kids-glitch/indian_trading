# Strategy registry and the benchmark zoo

## Strategy registry (`research/registry.py`)

The registry is the **only** way research selects a strategy. An AI agent
(or any caller) may select a strategy by *registered id* with *predefined
parameter values*; it may never submit code.

- `STRATEGY_REGISTRY` is built once at import from Python code and wrapped
  in `types.MappingProxyType` — mutation raises `TypeError` at runtime.
- `StrategyRegistry.build(registry_id, parameters)` validates every
  parameter against the entry's declared bounds, fills canonical defaults,
  and only then calls the code-defined factory.
- Unknown ids raise `UnknownStrategyError`; out-of-bounds parameters raise
  `ParameterOutOfBoundsError`.
- `registry.context()` is the AI-facing summary (ids, families, canonical
  parameters, bounds).

### Registered strategies (canonical configuration)

| registry_id | family | canonical parameters | bounds (selected) |
| --- | --- | --- | --- |
| `momentum` | momentum | lookback 63, threshold 0.0 | lookback [20, 260] |
| `crossover` | trend | fast 20 / slow 50, sma | fast [5,60], slow [50,260] |
| `mean_reversion` | mean_reversion | window 20, z −1.0 | window [5,60], z [−3,−0.1] |
| `momentum_quality` | momentum_quality | 63 / 0.25 / 0.50 (**frozen v0.6 baseline**) | lookback [42,126], quantiles [0.05,0.95] |
| `cross_sectional_momentum` | momentum | lookback 126, quantile 0.25 | lookback [42,250], quantile [0.05,0.5] |
| `trend_following` | trend | slow_window 200, sma | slow [100,250] |
| `quality` | quality | quantile 0.50 | quantile [0.05,0.95] |
| `low_volatility` | volatility | window 63, quantile 0.25 | window [20,120], quantile [0.05,0.5] |
| `reversal` | mean_reversion | window 20, quantile 0.25 | window [5,60], quantile [0.05,0.5] |

The frozen v0.6 baseline (`momentum_quality` with 63 / 0.25 / 0.50) is
canonical; any deviation is a **new research experiment**, never a
modification of the baseline.

Every cross-sectional strategy applies the point-in-time membership mask
**before ranking** (`_QuantileRankStrategy._rank_select`). This is the
mask-then-rank contract that the leakage audits enforce.

## Benchmark zoo (`research/zoo.py`)

Ten pre-declared families, all run under one methodology — same engine,
same cost model, same rebalance frequency, same universe mask, same
portfolio constraints:

1. `buy_and_hold` — equal initial allocation, never rebalanced
2. `equal_weight` — equal weights, monthly rebalance
3. `inverse_volatility` — 20-day realized-volatility inverse weighting
4. `random` — seeded random weights (deterministic placebo)
5. `persistence` — previous month's cross-sectional-momentum selection
   held unchanged (one-month stale signal)
6. `cross_sectional_momentum` — top-quantile 126-day trailing returns
7. `trend_following` — assets above their 200-day average
8. `quality` — top-quantile composite fundamental quality (requires the
   point-in-time fundamentals frame)
9. `low_volatility` — lowest-quantile 63-day realized volatility
10. `mean_reversion` — most-oversold quantile by 20-day price z-score

`run_benchmark_zoo(...)` returns results keyed by family id;
`WeightPanelStrategy` + `IdentityConstructor` let the weight-based
families run through the same walk-forward/CPCV validation machinery as
the signal-based families.

The zoo is methodological validation, not a grid search: families are
pre-declared with canonical parameters, and adding a family is a code
change with a regression test.
