# Storage Guide

Storage is split into raw Parquet files and an analytical DuckDB layer.

## Parquet
The `StorageManager` writes monthly Parquet partitions to avoid large monolithic files.
`data/raw/{source}/{exchange}/{symbol}/{YYYY}/{MM}.parquet`

When new data is ingested, the engine intelligently loads the existing month partition, appends the new data, drops duplicates keeping the most recent records, and writes back. This prevents corruption and allows idempotent fetches.

## DuckDB
`DuckDBManager` creates an in-memory or persisted database (configured at `data/quant.duckdb`) and registers a global `market_data` view using globs (`read_parquet('data/raw/*/*/*/*/*.parquet')`).

### Snapshots
You can create a point-in-time snapshot using the CLI:
```bash
python main.py snapshot --name daily_run_01
```
This writes a single `.parquet` file in `data/snapshots/` containing the exact state of the `market_data` view.
