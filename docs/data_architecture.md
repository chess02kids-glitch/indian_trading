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
