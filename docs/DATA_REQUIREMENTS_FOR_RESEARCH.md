# Data Requirements for Backtest Research

This is the complete list of what the research layer needs to move a
*candidate* strategy into a *validated* one. Every item says:

* **I have it now** / **MISSING**
* **What it unlocks**
* **Expected file path**
* **Expected schema / format**
* **Priority**

> **GitHub note before you upload:** the repository's `.gitignore` currently
> ignores `data/raw/*`, `data/features/*`, `data/clean/*`, `data/bundle/*`,
> `data/snapshots/*`, and `data/*.duckdb`. If you want raw data committed to
> GitHub you must either force-add it or amend the ignore rules. Big Parquet
> files (especially multi-year EOD, tick, option, or fundamentals history)
> will bloat the repo; a better pattern is a separate **data branch / LFS /**
> **DVC / cloud bucket**, and keep only tiny, versioned memberships and
> provenance CSVs in-tree. The loader paths below are read from
> `data/...`, so a mounted/MVP data drive at the same paths also works.

---

## 1. Long price history — **highest priority**

### Current: `data/raw/eod2_data/NSE/{symbol}/{year}/{month}.parquet`
**STATUS: HAVE IT** (133 symbols, 2023-01-02 → 2026-08-21, adjusted OHLCV, volume).
Already used in all the backtests above. Keep as the baseline.

### Missing: multi-year history
| | Expected path | Schema (columns) | Frequency | Coverage | Notes |
|---|---|---|---|---|---|
| **EOD adjusted daily OHLCV** | `data/raw/eod2_data/NSE/{symbol}/{year}/{month}.parquet` | `date, symbol, open, high, low, close, volume, series, source, exchange, ingested_at, source_ts, adjustment_state` | Daily | **2015-01-01 → latest** | The single most important gap. Unblocks cross-regime tests, true out-of-sample, walk-forward, and reduces overfitting to one bull window. |
| **EOD unadjusted raw** (optional duplicate) | `data/raw/eod_{source}/...` | `date, symbol, o/h/l/c, volume` | Daily | same | Needed to independently verify split/bonus adjustment and corporate-action effect. |

**Unlocks:** robust OOS/walk-forward, regime/vol analysis, macro-regime splits,
annualized risk estimates across ≥2 market cycles.

---

## 2. Historic fundamentals — **high priority**

### Current: `data/bundle/fundamentals_quarterly.parquet`
**STATUS: HAVE A SNAPSHOT ONLY** — 6 rows, all dated `2026-08-29`
(`date, symbol, roe, debt_to_equity, fiscal_quarter_end, source, fetched_at`).
This is **not** a time series; it cannot backtest value/quality/Momentum+Quality.

### Missing: full quarterly fundamentals time series
| | Expected path | Schema | Coverage |
|---|---|---|---|
| **Fundamentals panel** | `data/bundle/fundamentals_quarterly.parquet` | `date, symbol, roe, debt_to_equity, pe, pb, market_cap, sales, net_income, revenue_growth_yoy, earnings_yoy, roa, gross_margin, operating_margin, dividend_yield, sector, industry, source, fetched_at` | **Quarterly, 2015-01-01 → latest**, per symbol, with fiscal-quarter end. |
| **Corporate / PIT fundamentals snapshots** | `data/bundle/fundamentals_pit.csv` (or per-quarter Parquet) | `as_of_date, symbol, field, value` | as-of/report-date aligned, to avoid look-ahead in fundamentals. |

**Unlocks:** Value factor, Quality factor, Momentum+Quality, low-vol+quality,
factor-combination strategies, and proper company-level risk screens.

---

## 3. Point-in-time universe + membership — **high priority**

### Current: `data/universe/nifty100-pit/nifty100.csv`
**STATUS: HAVE IT** (210 membership windows / 175 symbols, Nifty 100 only,
ISIN + delisted flag). **Missing:** Nifty 50 / 500 PIT, coverage before 2023,
and exact reconstitution dates.

### Missing:
| | Expected path | Schema | Coverage |
|---|---|---|---|
| **Nifty 50 PIT** | `data/universe/nifty50-pit/nifty50.csv` | `symbol, index_name, valid_from, valid_to, isin, sector, exchange, delisted` | **2010-01-01 → latest** |
| **Nifty 500 PIT** | `data/universe/nifty500-pit/nifty500.csv` | same | **2010-01-01 → latest** |
| **Index reconstitution history** | `data/universe/reconstitution_events.csv` | `index_name, effective_date, added[], removed[], reason` | full available history |

**Unlocks:** reduced survivorship bias, accurate backtests for Nifty50/100/500
universes, sector/industry constraints, and exact point-in-time membership.

---

## 4. Market indices / regime / breadth — **medium-high**

### Current: **none** as a usable time-series (only the wide price snapshot).

### Missing:
| | Expected path | Schema | Frequency |
|---|---|---|---|
| **Benchmark indices** | `data/market/indices/*.parquet` | `date, index_name, open, high, low, close, returns` | Daily |
| **India VIX** | `data/market/india_vix.csv` | `date, close, high, low` | Daily |
| **Market breadth** | `data/market/breadth.csv` | `date, advances, declines, unchanged, adv_decl_ratio, breadth_pct` | Daily |
| **Sector indices** | `data/market/sector_indices/*.parquet` | `date, sector_index_name, close` | Daily |

**Unlocks:** beta/alpha estimation vs proper benchmark, market-neutral hedging,
vol-targeting, regime classification, drawdown attribution, and whether returns
are just beta.

---

## 5. Intraday data — **medium-high (needed for ORB/gap/intraday)**

### Current: **none**.

### Missing:
| | Expected path | Schema | Notes |
|---|---|---|---|
| **5-min / 15-min OHLCV** | `data/intraday/{symbol}/{YYYY-MM-DD}.parquet` | `date, symbol, open, high, low, close, volume` | minimum for opening-range breakout, gap-reversion, intraday execution. |
| **Tick / trade-level** | `data/intraday/tick/{symbol}/{YYYY-MM-DD}.jsonl` or parquet | `ts, price, volume, side` | for realistic slippage and execution. |
| **Order book snapshots** | `data/market/orderbook/{symbol}/{YYYY-MM-DD}.parquet` | `ts, bid1, bid_size, ask1, ask_size, ...` | best for adverse-selection/fill modeling. |

**Unlocks:** intraday strategies, realistic fill/slippage, ORB, gap fade,
open-high-low range stat-arb, execution costs beyond the fixed-bps model.

---

## 6. Short/borrow data — **medium-high**

### Current: **none**.

### Missing:
| | Expected path | Schema |
|---|---|---|
| **Borrow availability / lending** | `data/market/borrow.csv` or per-day parquet | `date, symbol, borrow_available, borrow_fee_pct, shortable, locates_available, settle_date` |
| **Short interest** | `data/market/short_interest.csv` | `date, symbol, short_interest, pct_of_float, days_to_cover` |

**Unlocks:** making long-short / market-neutral results **tradable and
realistic**. Without borrow data, our long-short backtests are research-only
because they assume unlimited, free shorting.

---

## 7. Options / volatility — **medium (for derivatives)**

### Current: **none**.

### Missing:
| | Expected path | Schema |
|---|---|---|
| **Option chain (NIFTY & stocks)** | `data/options/chains/{date}/{symbol}.parquet` | `symbol, expiry, strike, type(C/P), open_interest, volume, bid, ask, last, implied_vol, delta, gamma, vega, theta, underlying_price` |
| **Volatility surface** | `data/options/vol_surface_{date}.parquet` | `underlying, expiry, strike, iv` |
| **Options trade / quote feed** | `data/options/trades/...` | `ts, symbol, expiry, strike, type, price, volume` |

**Unlocks:** options strategies, volatility premium, multi-leg structures,
delta/vega hedging, and realistic derivative simulation. The current engine is
**not** built for this yet — that will be a legitimate engine rework.

---

## 8. Sector / industry + market-cap/float — **medium**

### Current: ISIN + partial sector in PIT CSVs; **no full S&P/NSE sector-class panel** and **no market cap**.

### Missing:
| | Expected path | Schema |
|---|---|---|
| **Sector & industry mapping** | `data/universe/sector_map.csv` | `symbol, sector, industry, index_membership, bse_isin, nse_ticker` |
| **Market cap / float** | `data/market/float.csv` or fundamentals panel | `date, symbol, market_cap, free_float, shares_outstanding, avg_daily_traded_value` |
| **Liquidity / turnover ranking** | `data/market/liquidity.csv` | `date, symbol, avg_traded_value_20d, turnover_ratio` |

**Unlocks:** sector-neutral constraints, avoid too much concentration, proper
liquidity-aware portfolio construction, small/mid/large-cap factor tests, and
realistic capacity estimates.

---

## 9. Corporate actions / event data — **medium**

### Current: prices are marked `split_bonus_adjusted`; **no event ledger**.

### Missing:
| | Expected path | Schema |
|---|---|---|
| **Corporate actions** | `data/corporate_actions.csv` | `symbol, ex_date, type(split/bonus/rights/merger/demerger/spin), ratio, description, adjust_date` |
| **Mergers / demergers / spin-offs** | `data/corporate_actions/mergers.csv` | `symbol, event_date, into_symbol, ratio, delist_date` |
| **Dividends** | `data/corporate_actions/dividends.csv` | `symbol, ex_date, amount, frequency, total_yield` |

**Unlocks:** verifying adjusted prices, correctly handling HDFC-style mergers,
and risk-adjusting for event-driven jumps.

---

## 10. Macro / country-level (optional, for regime)

### Missing (nice-to-have):
| | Expected path | Schema |
|---|---|---|
| **Macro time series** | `data/market/macro.csv` | `date, repo_rate, 10y_gilt_yield, inr_usd, crude_price, fii_dii_flow, cpi, gdp_growth` |

**Unlocks:** regime detection, macro-overlay, and better risk budgeting.

---

## Summary priority table

| # | Dataset | Status | Priority | Blocks |
|---|---|---|---|---|
| 1 | Full EOD adjusted OHLCV 2015+ | **MISSING** | 🔴 P0 | Robust OOS / walk-forward / cross-regime |
| 2 | Historic fundamentals quarterly | **MISSING** (snapshot only) | 🔴 P0 | Value / quality / factor strategies |
| 3 | Nifty 50/100/500 PIT + reconstitution | **PARTIAL** (Nifty100 only) | 🟠 P1 | Survivorship-free universe |
| 4 | Index, VIX, breadth, sector indices | **MISSING** | 🟠 P1 | Beta/alpha, regime, market-neutral |
| 5 | Intraday / tick / order book | **MISSING** | 🟠 P1 | Intraday, realistic fills |
| 6 | Borrow / short data | **MISSING** | 🟠 P1 | Tradable long-short |
| 7 | Option chains / IV | **MISSING** | 🟡 P2 | Options/vol strategies |
| 8 | Sector + market cap + float | **PARTIAL** | 🟡 P2 | Sector-neutral, liquidity-aware, small-cap factor |
| 9 | Corporate actions | **MISSING** | 🟡 P2 | Event risk, merger handling |
| 10 | Macro | **MISSING** | 🟢 P3 | Regime overlays |

**Minimum to make the current candidate evidence-backed:** items **1, 3, 4**
(first). **Minimum to test value/quality/factor investing:** items **2, 3, 8**.
**Minimum to validate intraday/orb/gap:** items **5**. **Minimum for tradable
market-neutral:** items **3, 4, 6**.
