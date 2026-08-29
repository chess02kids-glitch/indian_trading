# Indian Trading — Research Report (2026-08-30)

**Goal:** run broad, hypothesis-driven backtests on the locally available
Indian market data, then validate any candidate adversarially (costs, OOS,
parameter robustness, concentration, liquidity, look-ahead).

**Headline:** the local engine is fully usable for *price-based* research, but
after a 37-strategy discovery sweep and deep validation we do **not** have a
robust, investable strategy yet. The strongest apparent signal (Bollinger
mean-reversion buying extreme losers) survives cost/OOS/parameter tests but is
**concentrated and fragile**, and disappears when forced to be diversified.

---

## 1. What data is available locally (verified)

| Dataset | Path | Availability |
|---|---|---|
| NSE daily OHLCV (long form, adjusted) | `data/raw/eod2_data/NSE/{symbol}/{year}/{month}.parquet` | **DATA AVAILABLE** — 133 symbols, 5,686 monthly files, ~116k rows, 2023-01-02 → 2026-08-21 |
| Nifty 100 point-in-time membership | `data/universe/nifty100-pit/nifty100.csv` | **DATA AVAILABLE** — 210 windows / 175 symbols, survivorship-protected |
| Nifty 50 / 100 / 500 current memberships | `data/universe/*.csv` | **AVAILABLE (current only, not windowed)** |
| Wide close-price snapshot panel | `data/raw/nifty100_ohlcv.parquet` | **AVAILABLE** — 96 symbols, 2024-08-30 → 2026-08-28 |
| Fundamentals (ROE / D/E) | `data/bundle/fundamentals_quarterly.parquet` | **DATA MISSING** — only 6 rows / 1 snapshot (2026-08-29). No history. |
| Intraday / order book | — | **DATA MISSING** |
| Bid / ask, borrow availability, option chains, IV | — | **DATA MISSING** |

**Data & universe quality that matters:**
- 6 weekend bars were found in the EOD2 files and are now dropped in the
  research loader.
- The PIT Nifty 100 universe has **134 active members** during 2023–2026;
  **133** have local price data. The only missing name is **HDFC** (merged
  into HDFCBANK in 2023) — a small, quantified survivorship gap.
- The correct research universe is therefore the **133 PIT-active symbols with
  local data** (including recent IPOs/spins such as JIOFIN, SWIGGY, TATACAP,
  BAJAJHFL, etc., which enter at their PIT membership date).

---

## 2. Research infrastructure added

Independent from the production engine, under `research/explorer/`:

- `io.py` — loads/cleans EOD2 into cached wide panels; PIT membership mask;
  research-universe resolver.
- `sim.py` — transparent, deterministic monthly/period target-weight simulator
  with next-day execution and turnover cost.
- `strategies.py` — 37 signal generators across 12 families.
- `run_discovery.py` — broad sweep (writes `data/features/discovery_sweep.csv`).
- `validate.py` — train/test, walk-forward, parameter sensitivity
  (`data/features/validation_report.json`).
- `bollinger_validation.py` — deep cost/OOS/beta-hedge/long-short checks
  (`data/features/bollinger_validation.json`).
- `risk_detail.py` — monthly P&L, rolling risk, position concentration,
  liquidity proxy (`data/features/risk_detail.json`).
- `diversified_reversion.py` — tests forced diversification
  (`data/features/diversified_reversion.json`).

Timing rule: signals are computed on close at rebalance date and simulated on
the **next** observation, so there is no same-bar look-ahead. All results are
with PIT membership masking and a conservative 12 bps one-way cost (0–40 bps
also tested for the candidate).

---

## 3. Discovery sweep (full window 2023-01-02 → 2026-08-21)

37 candidates, rebalanced monthly, 12 bps one-way.

| Strategy | Family | CAGR | Vol | Sharpe | Max DD |
|---|---|---|---:|---:|---:|---:|
| **boll_30_2.5** | Mean reversion | 33.6% | 19.4% | **1.73** | −24.4% |
| invvol_63d | Inverse volatility | 14.9% | 13.2% | 1.13 | −20.9% |
| invvol_20d | Inverse volatility | 14.7% | 13.4% | 1.10 | −20.9% |
| rsi_14_30 | Mean reversion | 22.3% | 20.6% | 1.08 | −22.8% |
| ts_mom_20d | Time-series momentum | 17.1% | 16.2% | 1.05 | −24.2% |
| **equal_weight** | Benchmark | 14.4% | 14.5% | 0.99 | −21.8% |
| orb_c_0.25–1.0 | Opening-range breakout | 21.9% | 23.5% | 0.93 | −33.3% |
| xs_mom_20d | Cross-sectional momentum | 15.0% | 16.1% | 0.93 | −24.8% |
| ma_trend_20_100 | Moving-average trend | 15.3% | 16.7% | 0.92 | −23.2% |
| Low-vol long-short / momentum long-short | Long-short | −1% … −3% | ~5% | ≤ −0.3 | −13% |

**Key discovery observations:**
- Long-only mean reversion and inverse-volatility outperformed the equal-weight
  benchmark on the full period.
- Cross-sectional **long-short** variants failed (near zero or negative),
  indicating no strong market-neutral cross-sectional edge in this window.
- Momentum/trend strategies were strong only in 2023–24 and decayed in 2025–26.

---

## 4. Adversarial validation

### 4.1 Full-period split (train 2023–24, test 2025–26)

| Strategy | Train Sharpe | Test Sharpe | Test CAGR | Verdict |
|---|---:|---:|---:|---|
| **boll_30_2.5** | 1.26 | **2.28** | 55.6% | Strong on both |
| invvol_63d | 2.02 | 0.34 | 5.1% | **Decayed** |
| rsi_14_30 | 0.07 | 2.21 | 51.5% | **Inconsistent** (failed train) |
| ts_mom_20d | 1.79 | 0.10 | 1.7% | **Failed OOS** |
| xs_mom_20d | 1.79 | −0.06 | −1.0% | **Failed OOS** |
| ma_trend_50_200 | 1.98 | −0.27 | −4.6% | **Failed OOS** |
| equal_weight | 1.65 | 0.35 | 5.5% | benchmark |

### 4.2 Bollinger mean-reversion deep-dive

- **Cost robustness:** 30/2.5 keeps Sharpe ≥1.51 even at 40 bps one-way
  (CAGR still ~29.5%). 20/2.5 keeps Sharpe ≥1.93 at 40 bps.
- **Parameter plateau:** the full 20-cell grid shows a cluster of strong
  configs around windows 15–40 and std 2.5–3.0 (Sharpe 1.0–2.1). Not a single
  isolated point.
- **Beta / alpha:** 30/2.5 has beta ≈ 0.40 vs equal-weight; annualized alpha
  ≈ +24%. A beta-hedged long-only version has Sharpe ≈ 1.35. So it is not
  purely market beta.
- **Out-of-sample:** a strict test (parameters chosen only on 2023–24 from the
  full grid, then evaluated 2025–26) picked 20/2.5; test Sharpe 1.81, CAGR
  30.9%, max DD −7.0%.

### 4.3 Why it is still NOT a validated strategy

The risk detail exposes the real structure:

| Config | Avg positions | Max positions | Worst month | Rolling-min Sharpe |
|---|---:|---:|---:|---:|
| 15/3.0 | **1.06** | 3 | −0.1% | −1.41 |
| 20/2.5 | **1.35** | 7 | −5.4% | −1.46 |
| 30/2.5 | 1.39 | 9 | −17.8% | −0.61 |

- The portfolio is **mostly in cash** and, when active, often holds **one or
  two names at 100%**.
- Best months are 29–32% (e.g. Apr–May 2026); the return is concentrated in a
  handful of extreme "panic-bounce" months.
- **Market-neutral / relative long-short is negative** (Sharpe −0.57 at 0 bps,
  −0.94 at 12 bps). So the edge is **not** broad cross-sectional reversal.
- **Diversification destroys the edge:** requiring ≥5 positions, the best
  diversified short-term-reversion screen is RSI-14 with a 5-name cap
  (Sharpe ≈ 1.43, avg 3.2 positions), and the z-score long-short variants are
  near zero.

**Interpretation:** the apparent alpha is a concentrated, low-frequency
"buy-the-extreme-loser" tilt that depends on a small number of names and
months in a strong equity regime. It is economically plausible (short-horizon
reversal/panic rebound) but **not evidence of a robust, repeatable edge** yet.

---

## 5. Diagnosis (honest)

Why the strongest discovery signal is not yet a survivor:

1. **Strategy universe / concentration** — 1–2 name bets; not a real
   diversified portfolio.
2. **Data length** — 3.7 years, one largely up-trending regime; insufficient
   for cross-regime confidence.
3. **Research process** — broad discovery is done; validation on this window
   is reasonably strict but the **registry of true OOS data is thin**.
4. **No market-neutral edge** — the cross-sectional long-short tests are
   negative, so the alpha is likely regime-long-biased.
5. **Data gaps** — no historical fundamentals, no borrow data, no intraday,
   no option chains. On the current data, fundamental/quality, options, and
   intraday strategies **cannot be rigorously validated**.

We therefore do **not** claim a winning strategy. We have a **candidate
research direction worth more data + more period coverage**, not a validated
rule.

---

## 6. Risk documentation

All results above (and the exploration code) report, at minimum:

- Max historical drawdown
- Worst single month
- Daily VaR95 / expected-shortfall (in `risk_detail.json`)
- Turnover and cost drag
- Beta/correlation vs equal-weight benchmark
- Position concentration (min/max/mean count, max single weight)
- Liquidity proxy (portfolio vs universe median traded value; selected names
  are liquid: ratio ≈ 0.9–1.08× universe)

**Explicitly flagged risks:** gap risk on panic names, single-name
concentration, no borrow/shorting data (long-short is research-only),
potential regime dependence, and survivorship gap limited to HDFC.

---

## 7. Next steps (what would move the needle)

1. **Extend price history** to ≥10 years (e.g., EOD2/NSE pre-2023) to get
   cross-regime evidence for mean-reversion/trend/volatility families.
2. **Obtain historical fundamentals** (ROE, D/E, sales, P/B, earnings) to test
   value/quality/factor strategies properly.
3. **Obtain borrow availability** to make long-short/market-neutral tradable.
4. **Obtain intraday/order-book** data to validate ORB / gap / intraday
   execution at realistic fills.
5. **Add a formal walk-forward engine** with purged/embargoed CV and deflated
   Sharpe on the 10-year dataset.
6. **Extend the engine** to multi-leg/derivatives once option data exists; the
   production `backtest` engine is currently not built for that and should be
   reworked there as needed.

---

## 8. Reproduction

```
# venv with pandas, numpy, duckdb, pyarrow, scipy
python -m research.explorer.run_discovery                # 37-strategy sweep
python -m research.explorer.validate                     # split/walk-forward/sensitivity
python -m research.explorer.bollinger_validation         # cost/OOS/beta-hedge/L-S
python -m research.explorer.risk_detail                  # concentration/rolling/monthly
python -m research.explorer.diversified_reversion        # forced diversification
```

Machine-readable outputs are in `data/features/`.
