# Strategy registry

`research.registry` is the only place an AI or script may obtain a strategy.

## Contract

Every registered strategy exposes:

* `name`, `family`, `version`
* `parameters`, `metadata()`
* `generate_signals(MarketData) -> Signal`

Signal metadata always includes `strategy_name`, `strategy_family`,
`strategy_version`, `parameters`, `feature_set`, and `signal_timestamp`.

The same object is reused by historical backtest and paper simulation.
There is no execution-mode branch in signal logic.

## Canonical zoo (one config each)

| Family | Canonical idea |
| --- | --- |
| `buy_and_hold` | equal initial allocation |
| `equal_weight` | equal weight of PIT-eligible names |
| `inverse_volatility` | 20-day realized vol, prefer low vol |
| `random` | seeded placebo ranks |
| `persistence` | 21-day return continuation |
| `momentum` | 63-day cross-sectional momentum, top quartile |
| `trend` | 50/200 MA crossover |
| `quality` | PIT ROE when fundamentals exist |
| `low_volatility` | 63-day realized vol, lowest quartile |
| `mean_reversion` | 20-day z-score reversal |

These are **not** a grid. Extra parameter sets are new experiments.

## PIT ranking

`research.pit.rank_eligible` masks ineligible names **before** ranking.
Ranking then masking is treated as a bug.

## Instantiation

```python
from research.registry import instantiate, BENCHMARK_ZOO
strategy = instantiate("momentum")  # canonical
strategy = instantiate("momentum", {"lookback": 126})  # NEW experiment
```

Unknown families raise `ResearchInputError`. There is no `exec` path.
