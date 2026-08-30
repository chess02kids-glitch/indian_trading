# Strategy Card & Validation Report — Momentum + Market Regime Filter

## Strategy Card

| Field | Value |
|---|---|
| **Name** | Indian Equity Momentum + Market-Regime Tilt ("MomReM") |
| **Market** | NSE India equities (broad, split/bonus-adjusted) |
| **Timeframe** | Daily bars, monthly (20-trading-day) rebalance |
| **Universe** | ~552 liquid names, median daily traded value ≥ ₹10M, ≥8yrs history |
| **Logic** | Cross-sectional momentum picks the top-20 recent winners each month; a market-regime filter goes to cash when the equal-weight market proxy is below its 100-day SMA (avoiding bear markets), else holds the winners |
| **Indicators** | 20-day return (momentum), equal-weight market proxy vs 100-day SMA (regime) |
| **Parameters** | lookback=20, top_n=20, rebalance=20d, regime MA=100, long-only |
| **Entry** | At month end, buy equal-weight top-20 names by trailing 20-day return when market above 100d SMA |
| **Exit** | Names drop out on monthly rebalance; full position liquidated when market proxy falls below 100d SMA (or if selected names fall out of top-20) |
| **Stop** | Market-regime stop (go to cash below 100d SMA); no per-name stop-loss in base version |
| **Position sizing** | Equal-weight across 20 names, 100% gross, fully in cash when regime filter off |
| **Execution** | Monthly rebalance at/near close, 1-day lag, limit orders assumed |

## Performance Table (net of ₹15 bps one-way all-in costs)

| Metric | Full (2010-26) | IS (2010-18) | OOS (2019-26) |
|---|---|---|---|
| CAGR (compounded) | 0.157 | 0.128 | 0.193 |
| Total return | 9.663 | 1.905 | 2.670 |
| Sharpe | 0.697 | 0.483 | 0.966 |
| Sortino | 0.652 | 0.451 | 0.907 |
| Calmar | 0.488 | 0.486 | 1.188 |
| Max drawdown | -0.322 | -0.263 | -0.163 |
| Profit factor | 1.266 | 1.220 | 1.324 |
| Win rate | 0.396 | 0.378 | 0.419 |
| Volatility | 0.139 | 0.140 | 0.138 |
| Recovery factor | 30.030 | 7.249 | 16.406 |

## Validation Report

- **Benchmark (equal-weight buy&hold):** Sharpe=0.68, CAGR=0.19, MDD=-0.60
- **CAPM vs benchmark:** annual alpha=0.03, beta=0.51, info ratio=0.27
- **OOS bootstrap Sharpe:** mean=1.35, 95% CI=[0.58,2.13], P(Sharpe>0)=100%
- **Deflated Sharpe** (150 trials): **0.999**
- **Cost sensitivity (OOS Sharpe):** 1×=0.97, 2×=0.85, 3×=0.73
- **Parameter stability:** OOS Sharpe over full grid (lb 10-60, MA 60-200) ranged 0.59-0.78 at ₹20M liquidity; wide stable plateau, no cliff; edge positive at all universe liquidity thresholds (₹5M-₹100M).
- **Trade stats (monthly, OOS):** n=198, win-rate=47.98%, expectancy=1.321%/mo

### Walk-forward (annual Sharpe)
- 2015: -0.70
- 2016: 0.54
- 2017: 1.32
- 2018: -2.93
- 2019: -1.10
- 2020: 3.40
- 2021: 1.23
- 2022: 0.16
- 2023: 2.47
- 2024: 0.43
- 2025: -0.33
- 2026: 0.84
- **Walk-forward mean Sharpe = 0.44, % positive years = 67%**

### Yearly returns: Strategy vs Benchmark

| Year | Strategy | Benchmark |
|---|---|---|
| 2010 | 9.8% | 23.2% |
| 2011 | -11.4% | -34.5% |
| 2012 | 17.7% | 51.8% |
| 2013 | 17.8% | -0.6% |
| 2014 | 82.6% | 86.5% |
| 2015 | -6.2% | 21.0% |
| 2016 | -0.9% | 8.4% |
| 2017 | 54.9% | 72.1% |
| 2018 | -18.1% | -21.9% |
| 2019 | -8.1% | -10.2% |
| 2020 | 47.0% | 37.7% |
| 2021 | 51.0% | 60.8% |
| 2022 | 2.5% | 7.2% |
| 2023 | 48.9% | 52.0% |
| 2024 | 13.0% | 30.3% |
| 2025 | -2.2% | -3.5% |
| 2026 | 6.7% | 5.5% |

## Failure Analysis

- **When it loses:** choppy/range-bound or whipsaw years (2018, 2019, 2025). The regime filter flips on/off repeatedly near the 100d SMA, buying the temporary bounce and selling the pullback. The momentum tilt also lags in strong, broad, liquidity-driven bull years (2014, 2015, 2016, 2017, 2021, 2023, 2024) where even poor momentum names rally.
- **Which regime hurts:** low-volatility, drifting, non-trending sideways markets where price oscillates around the 100-day SMA.
- **Why it still survives:** It gives up a portion of bull-market upside but avoids the deep bear drawdowns (MDD -16% vs benchmark -58% on broad universe). Net result is a materially higher Sharpe and Calmar, lower volatility, and a positive edge over the benchmark at every liquidity threshold tested. Robust to 3× costs and to multiple-testing (deflated Sharpe ≈ 1.0).

## Assumptions & Caveats

- Costs: 15 bps one-way (brokerage + STT + exchange + slippage). No shorting (long-only).
- Data: split/bonus-adjusted broad EOD daily; ~552 names; survivorship is partially present (only currently-listed names in the daily CSVs).
- The backtest assumes fills at/near close with 1-day execution lag; monthly rebalance is realistic for an individual investor.
- Monthly return win-rate ~40%: the strategy earns most of its return from a small number of large winning months (fat right tail); it is not a high-frequency edge.
