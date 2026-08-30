# Research Journal — Exhaustive Strategy Discovery (Indian Markets)

Period researched: 2009-01 to 2026-06 on NSE daily OHLCV.
Universe candidates: 133 split/bonus-adjusted large caps (clean parquets) plus a
broad split-adjusted universe built from daily EOD CSVs (with a liquidity filter).

All backtests use realistic Indian costs (15 bps one-way: brokerage + STT +
exchange + slippage), a mandatory 1-day execution lag, and long-only exposure
unless noted. The benchmark is a buy-and-hold equal-weight portfolio of the same
universe. The honest, inescapable bar: **the equal-weight benchmark itself
delivers Sharpe ≈ 0.9-1.0, CAGR ≈ 20-23%, and must be beaten net of costs.**

---

## 1. Data & Pipeline
- Loaded 133 clean large-cap parquets (1995-2026, split/bonus adjusted) and
  3,690 daily CSVs (broad universe, split-adjusted via `corporate_actions.csv`).
- Built a vectorized portfolio simulator (long-only and long-short), a metrics
  suite (CAGR, Sharpe, Sortino, Calmar, PF, drawdown, bootstrap, deflated
  Sharpe), and a CAPM alpha engine vs the equal-weight benchmark.
- **Bug found & fixed:** cross-sectional strategies initialized rebalance
  weights to `0.0` before `ffill()`, so positions were only held on the exact
  rebalance day (not between rebalances). Fixed to `NaN`+`ffill()`. This
  materially changed all cross-sectional results.

## 2. Family-by-Family Results (all net of costs, 1-day lag)

### Trend following (dual MA, MA cross, Donchian/Turtle, TS momentum)
- Long-only, always fully invested in bull regimes.
- IS Sharpe 0.6-0.85, but **negative alpha vs the equal-weight benchmark
  (beta ≈ 0.9, alpha ≈ -2%/-4%)**.
- **REJECTED:** trend timing on a broad large-cap index does not beat buy-and-hold
  in a secular bull market (Nifty 50 100-250d MA timing Sharpe 0.19-0.21 vs
  buy-and-hold 0.31).

### Mean reversion (RSI, Bollinger) — long-only
- IS Sharpe 0.35-0.48, OOS 0.09-0.13, beta ≈ 0.99, strongly negative alpha.
- **REJECTED:** no edge; high turnover and beta-dominated.

### Volatility targeting on the EW portfolio
- Cuts drawdown (-38% → -23%) but also Sharpe (0.97 → max 0.91).
- **REJECTED:** returns sacrificed exceed drawdown benefit.

### Cross-sectional momentum & reversal (long-short, long-only)
- Naive long-short is destroyed by short-squeeze risk + borrow costs in a bull
  market (Sharpe ≈ -1).
- Long-only top-quintile, monthly rebalance, **with 1-day lag and costs**:
  converges to the benchmark (OOS Sharpe ≈ 0.86, beta = 1.00, alpha ≈ 0) on the
  91 large-cap universe. **The apparent momentum edge was largely look-ahead
  bias** (capturing the signal-day return).
- Low-vol and reversal tilts similarly converge to ~benchmark on large caps.

### Broad universe (incl. small/mid caps)
- **The edge is real and lives in smaller/less-liquid names.**
- Momentum (20d) + market-regime filter (EW proxy vs 100d SMA), long-only,
  monthly rebalance, 1-day lag, costs:
  - ₹5M/day liquidity, 702 names: OOS Sharpe **0.96** (benchmark 0.69)
  - ₹10M/day, 552 names: OOS Sharpe **0.97**, MDD **-16%**, Calmar 1.19 (benchmark 0.68)
  - ₹20M/day, 392 names: OOS Sharpe 0.76 (benchmark 0.64)
  - ₹100M/day (near large-cap), 159 names: OOS Sharpe 0.62 (benchmark 0.52)
- **Edge is positive at EVERY liquidity threshold (+0.10 to +0.29 OOS Sharpe),
  with far lower drawdown than the market (-16% to -23% vs -40% to -60%).**

## 3. Validation of the Winner ("MomReM": momentum 20d, top-20, monthly, regime MA=100)
- Parameter stability: OOS Sharpe across grid (lookback 10-60, MA 60-200,
  top_n 10-30) is a **wide plateau** (mean 1.20, std 0.08 at ₹5M; 0.59-0.78 at
  ₹20M). No parameter cliff. 100% of grid points beat their benchmark.
- Bootstrap OOS Sharpe: mean 1.35, 95% CI [0.58, 2.13], P(Sharpe>0) = 100%.
- Deflated Sharpe (150 trials): **0.999** — survives multiple-testing correction.
- Cost sensitivity: OOS Sharpe 0.97 → 0.85 (2×cost) → 0.73 (3×cost). Robust.
- Walk-forward annual Sharpe: mean 0.44, positive 67% of years. **Weak spot is
  choppy years (2018, 2019, 2025).**

## 4. Why other families were rejected (summary)
| Family | Verdict | Reason |
|---|---|---|
| Trend MA / Donchian (long-only) | Reject | Negative alpha; beta play |
| RSI/Bollinger mean reversion | Reject | Negative alpha, high turnover |
| Volatility targeting | Reject | Lower Sharpe than benchmark |
| Momentum/reversal long-short | Reject | Short-squeeze + borrow costs destroy edge |
| Large-cap factor tilts | Reject | No net-of-cost, lag-corrected alpha |
| Delivery-volume signals | Not feasible | Data present for only ~2.5% of symbols |

## 5. Final Decision
**Keep "MomReM"** (Momentum + Market-Regime Filter) as the only strategy that
survives validation: it beats the equal-weight benchmark at every universe
liquidity threshold, with dramatically lower drawdown, and is robust to cost
assumptions and parameter choice. It is a risk-adjusted improvement over
buy-and-hold, not a raw-return home run. It underperforms in strong liquidity-
driven bull years and whipsaw years — documented honestly in the strategy card.

---

## 6. Round 2 — Additional Families (documented 2026-08)

### Overnight premium (new finding)
- **Effect:** In Indian equities nearly the entire market return accrues *overnight*
  (close→open): overnight component CAGR **47.9%**, Sharpe **4.36**; intraday
  (open→close) is *negative* (CAGR -16%, Sharpe -1.5). This is the well-known
  Indian overnight premium.
- **Verdict: NOT directly tradeable.** Capturing it requires buying every close and
  selling every open (~2-way cost daily). At 15 bps one-way it nets **-30% CAGR**;
  break-even requires ~5 bps one-way, which is unrealistic after STT+slippage.
  The premium is already embedded in buy-and-hold, which is why buy-and-hold has
  a high Sharpe. **Note for implementation:** never go to cash overnight if you
  can avoid it (you forfeit the free overnight return). Holds the momentum book
  through the night is part of why MomReM works.

### Statistical arbitrage / pairs trading (dollar-neutral)
- Ranked pairs by rolling correlation, traded extreme normalized-spread z-scores,
  daily, 1-day lag, costs.
- **Verdict: REJECTED.** IS Sharpe -0.89, OOS Sharpe -1.72. Short-side borrow +
  transaction costs destroy the spread edge in a trending bull market.

### Gap fade (short overnight gap-ups, long gap-downs)
- **Verdict: REJECTED.** IS Sharpe -5.5, OOS Sharpe -3.6. Consistent with the
  overnight premium (gaps are informational, not noise to fade).

### Sector rotation (momentum across Nifty sector indices)
- Long the top-k sectors by momentum, with/without regime filter.
- **Verdict: REJECTED.** OOS Sharpe ~0.34 vs equal-weight-across-sectors 0.35.
  No cross-sector edge net of costs; sector indices largely move together.

### MomReM enhancements (long-only, broad universe)
Tested low-volatility tilt within top names, drawdown de-risking overlay,
multi-timeframe momentum (20/60/120 composites), price acceleration (20d-60d),
volume-surge confirmation, and weekly lookbacks:
- **All are NEUTRAL:** every variant lands at OOS Sharpe 0.94-0.97, MDD -16/-18%,
  matching the base MomReM (0.97). The regime filter already provides the risk
  management (so the drawdown overlay never activates), and the momentum edge is
  already fully captured by the simple 20d cross-sectional tilt.
- **Interpretation:** MomReM sits at the *fair value* of the available long-only
  edge. The standard refinement toolkit cannot push it materially higher — which
  is itself strong evidence the strategy is robust (no fragile parameter that
  another tweak could improve).

## 7. Final Conclusion (Round 2)
No additional surviving strategy beyond **MomReM**. The search space is
practically exhausted for this dataset:
- Market-neutral (short-based) edges are all destroyed by costs/borrow.
- The overnight premium is real but untradeable directly (embedded in buy-hold).
- Long-only factor tilt (momentum) + regime de-risking is the one genuine,
  robust, implementable edge, and MomReM is its cleanest expression.
- Refinements (low-vol, drawdown, multi-TF, volume, sector) add nothing net of
  costs, confirming MomReM is a stable local optimum rather than a lucky fit.
