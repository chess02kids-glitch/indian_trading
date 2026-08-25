# Statistical validation engine

The validation layer (`backtest.validation`) turns raw backtest output into
statistical evidence. It is deterministic: identical inputs always produce
identical outputs, and no function touches the clock, the network, or any
global random state exposed to callers.

```
BacktestResult ──> Deflated Sharpe ──> Purged walk-forward / CPCV
                       │                        │
                       └──── Bootstrap CIs ─────┘
                            (Sh, CAGR, MDD, Vol, Turnover)
```

## Deflated Sharpe Ratio

`deflated_sharpe_ratio` implements the Bailey & López de Prado (2014)
adjustment:

* **Skew/kurtosis adjustment** — the asymptotic variance of the Sharpe
  estimator is `(1 - γ₃·SR + (γ₄ - 1)/4 · SR²) / (T - 1)`, so fat-tailed or
  negatively-skewed return distributions widen the standard error.
* **Expected maximum Sharpe** — `E[max SR] ≈ (1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(Ne))`
  scaled by the standard error; `N` is the number of *tested variants*.
* **Multiple-testing correction** — the probability that the observed Sharpe
  beats the lucky maximum of `N` trials is
  `Φ((SR - E[max SR]) / SE)`. More trials ⇒ a higher hurdle.

`deflated_sharpe_from_returns` derives the inputs from a return series with
bias-adjusted sample moments. `expected_maximum_sharpe` is exposed directly
so callers with non-independent trial families can supply their own hurdle.

## Purged walk-forward

`walk_forward_splits` creates rolling or expanding train/test windows with
two explicit leakage controls:

* **`purge`** — the last `purge` training observations are removed because
  their label look-back overlaps the first test observation.
* **`embargo`** — an additional gap between the purged training window and
  the test window.

`run_walk_forward` evaluates the strategy on each test slice only; fold
results never include training observations. Signals are computed once from
the point-in-time panel because every factor and constructor is trailing
only, so the value at `t` never depends on data after `t`.

## Combinatorial purged cross-validation

`combinatorial_purged_cv` divides the index into `N` contiguous groups and
constructs every `C(N, k)` combination of test groups. Training
observations within `purge` rows of any test group are removed and
`embargo` widens the exclusion. The construction is pure (no RNG), so:

* the same inputs always produce the same folds (reproducible),
* every window is fully independent (parallel-safe),
* `run_combinatorial_purged_cv` evaluates each path with the same
  point-in-time, zero-look-ahead discipline.

`validation_consistency` summarizes a walk-forward/CPCV run: the fraction of
folds with positive Sharpe, best/worst fold Sharpe, the dispersion of fold
Sharpes, and the aggregate multiple-testing-corrected probability.

## Bootstrap confidence intervals

`bootstrap_metric_intervals` produces percentile-bootstrap confidence
intervals for **Sharpe, CAGR, max drawdown, volatility, and annualized
turnover** with a configurable iteration count, confidence level, seed, and
optional circular-block resampling (`block_length`) that preserves
autocorrelation. One resample matrix drives every metric, so the intervals
are internally consistent. `bootstrap_sharpe_confidence_interval` remains
available as a backward-compatible wrapper.

All intervals are deterministic for a fixed seed; changing the seed changes
the interval (never the point estimate).
