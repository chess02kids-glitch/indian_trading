# Factor library

All technical factors implement the common `Factor` contract:

```python
factor.metadata  # FactorMetadata
values = factor.compute(MarketData(close=prices, high=high, low=low))
```

Outputs preserve the input date index and symbol columns. Warm-up periods are
`NaN` rather than backfilled; portfolio constructors treat unavailable signals
as inactive.

## Factor families

| Family | Implementations | Default parameters |
| --- | --- | --- |
| Momentum | `Momentum1MFactor`, `Momentum3MFactor`, `Momentum6MFactor`, `Momentum12MFactor`, `SharpeMomentumFactor` | 21, 63, 126, 252 trading days; 63/21 vol-scaled |
| Trend | `SMAFactor`, `EMAFactor`, `MovingAverageCrossoverFactor` | 20; 20/50 crossover |
| Mean reversion | `ZScoreFactor`, `BollingerDeviationFactor` | 20 observations; 2 standard deviations |
| Volatility | `RollingVolatilityFactor`, `ATRFactor` | 20 and 14 observations |
| Relative strength | `RelativeStrengthRankFactor` | 21-day momentum percentile |
| Quality | `models.QualityFactor` | Interface for point-in-time fundamentals |

## Semantics

- Momentum is close-to-close trailing return.
- SMA and EMA use only observations through the current row.
- Crossover returns `1` when the fast average is above the slow average and
  leaves the slow-average warm-up period unavailable.
- Z-score and Bollinger deviation measure distance from a rolling mean.
- Rolling volatility is annualized using 252 periods by default.
- ATR uses the maximum of high-low, high-prior-close, and low-prior-close true
  ranges.
- Relative-strength rank is cross-sectional per date, with the strongest
  asset receiving the highest percentile.

Factor metadata includes name, family, description, version, and all parameters,
which are passed into experiment tracking for reproducibility.
