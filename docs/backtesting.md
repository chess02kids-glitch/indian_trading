# Backtesting guide

## Running a simulation

The engine consumes positive close prices and a date/symbol target-weight
panel. It is independent of notebooks and accepts any pandas-compatible
rebalance frequency:

```python
from backtest import BacktestConfig, VectorBTResearchEngine
from research import CostModel

config = BacktestConfig(
    rebalance_frequency="M",
    cost_model=CostModel(transaction_cost_bps=5, slippage_bps=2),
    use_vectorbt=True,
)
result = VectorBTResearchEngine(config).run(prices, target_weights, "momentum")
```

`M` means the last available observation of each calendar month; the first
available observation is always treated as an initial rebalance. Targets are
forward-filled between rebalances and shifted one observation before returns,
which prevents same-bar look-ahead.

## Costs and turnover

Turnover is the sum of absolute target-weight changes at rebalances. Transaction
cost and slippage are charged independently in the trade ledger and are both
included in returns. Benchmarks receive the same `BacktestConfig`, so their
comparisons use identical costs.

## Volatility targeting

Set `volatility_target` and `volatility_lookback` to activate the hook. The
engine estimates annualized volatility from returns before each rebalance and
scales the target subject to `max_leverage`. With no sufficient history, the
unscaled target is used.

## Backend behavior

VectorBT is the preferred backend. The engine uses
`vectorbt.Portfolio.from_orders` with target-percent orders, shared cash, fees,
and slippage. If VectorBT cannot import or cannot process a particular
numerical input, the deterministic pandas simulator is used and the result
metadata records `backend="pandas"`; a warning is logged.

## Standard benchmarks

`benchmark_suite()` always creates:

- `buy_and_hold`
- `equal_weight`
- `inverse_volatility`
- `random` with an explicit seed
- `persistence`

`compare_results()` returns one standardized metric row per strategy or
benchmark.
