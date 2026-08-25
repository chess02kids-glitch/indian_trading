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
