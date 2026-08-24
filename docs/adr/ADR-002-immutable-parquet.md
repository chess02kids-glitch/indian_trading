# ADR-002: Immutable Parquet Storage

## Context
Market data (OHLCV) is ingested daily. Modifying existing historical files leads to data corruption, race conditions during backtests, and poor reproducibility. 

## Decision
All market data will be stored as immutable Parquet files partitioned by `source/exchange/symbol/year/month.parquet`. 

## Alternatives Considered
- CSV: Human-readable but slow to parse, uncompressed, lacks schema enforcement.
- HDF5: Good for numerical data but less ecosystem support across modern data tools than Parquet.
- Time-series DB (Influx/Timescale): Operational overhead for a VPS setup.

## Consequences
- **Pros**: Highly compressed, columnar reads are optimized, schema is strictly enforced, integrates seamlessly with DuckDB.
- **Cons**: Updates require rewriting an entire partition (month file), which is fine since historical market data rarely changes.

## Future Review Criteria
If tick-level data (L2/L3 order book) ingestion becomes required, Parquet partitioning might need to move from monthly to daily to manage file sizes.
