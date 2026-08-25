# Data Architecture Guide

## Storage Layer
The data platform relies on **Parquet** files as the source of truth for all historical market data. 
Files are partitioned by source, exchange, symbol, year, and month to allow fast localized reads and appending.

Path format:
`data/raw/{source}/{exchange}/{symbol}/{YYYY}/{MM}.parquet`

## Analytical Layer
**DuckDB** sits on top of the Parquet storage, providing an analytical interface to the raw files.
- Automatically creates a view `market_data` which unions all historical data.
- Supports taking zero-copy snapshots.
- Fast execution of SQL directly on parquet files.

## Ingestion & Validation
The pipeline orchestrates fetching from providers (like `yfinance`), normalizing data, running strict schemas via `pandera`, and saving valid datasets into Parquet.

### Quality gates (`data.quality.check_ohlcv_long_frame`)

Every incoming long OHLCV frame passes a deterministic, report-based
quality gate. Invalid rows are **flagged and excluded — never silently
repaired or imputed**:

* unparseable timestamps, missing/empty symbols;
* missing, non-positive, or non-finite OHLC prices; negative/missing volume;
* impossible OHLC relationships (`high >= max(open, close)`, `low <=
  min(open, close)`, `high >= low`);
* duplicate `(date, symbol)` rows;
* **future observations** — pass `as_of=<reference date>` to flag and
  exclude rows dated strictly after the reference, so research and
  execution can never see data that has not happened yet;
* stale data (`detect_data_staleness`) and unexpected calendar gaps
  (`detect_missing_candles`, `detect_off_calendar_candles`) are reported
  separately so the caller decides.

## Real data (v0.7)

The v0.7 milestone adds a **real-data** path on top of the same layers:

* **Price source**: `eod2_data` (NSE official daily reports mirror),
  ingested via `ingestion/eod2_adapter.py` into the canonical long contract
  (`symbol, exchange, date, OHLC, volume, source, ingested_at` +
  `source_ts`, `adjustment_state`). `ingested_at` is the source
  `meta.json lastUpdate` (deterministic), not the wall clock.
* **Raw layer**: `data/raw/eod2_data/NSE/<SYM>/<YYYY>/<MM>.parquet` —
  window-scoped normalised source rows (full history stays pinned at the
  source commit).
* **Clean layer**: `data/clean/eod2_data/<SYM>.parquet` + `.meta.json`
  (row count, fingerprint, quality issues) via `CleanDataCatalog`; the
  quality gate excludes invalid/duplicate/future rows and *reports* them —
  never repairs.
* **PIT universe**: `data/universe/nifty100-pit/` (CC BY 4.0 source; CSV +
  `provenance.json` with commit, source SHA-256, membership fingerprint,
  per-row NSE press-release URLs). The strategy ranks cross-sectional
  factors only within each date's actual members (`active_members` mask).
* **Operator bundle**: `data/bundle/` (git-ignored) — yfinance quarterly
  ROE/debt-to-equity with a conservative next-quarter-end availability date,
  plus an independent raw-close cross-check JSON. Produced by the single
  external-data command `python scripts/ingest_real_data.py
  --fetch-fundamentals` (see `docs/real_data.md`).
* **Research assembly**: `research/realdata.py` builds the rectangular
  maximum-clean-window panel, the PIT mask, and the §7 completeness report;
  `scripts/run_real_data_experiment.py` re-runs the exact frozen v0.6
  baseline and asserts the configuration has not drifted.
