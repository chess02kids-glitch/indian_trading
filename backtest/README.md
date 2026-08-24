# backtest

The backtest package implements deterministic target-weight simulations for
research. `VectorBTResearchEngine` uses VectorBT when its numerical backend is
available and retains a transparent pandas implementation as an explicit
fallback.

The engine supports monthly or other pandas-compatible rebalance frequencies,
equal-weight and inverse-volatility allocations, transaction costs, slippage,
turnover, and an optional volatility-targeting hook. Returns use weights from
the prior observation to prevent same-bar look-ahead.

The benchmark suite always runs Buy and Hold, Equal Weight, Inverse Volatility,
Random, and Persistence baselines through the same engine and cost model.
Temporal validation includes rolling/expanding walk-forward windows,
combinatorial purged cross-validation, deflated Sharpe, and deterministic
bootstrap confidence intervals.

See [`docs/backtesting.md`](../docs/backtesting.md) and
[`docs/validation.md`](../docs/validation.md).
