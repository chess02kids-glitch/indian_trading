# ADR-001: DuckDB as Analytical Database

## Context
Quant India processes massive amounts of historical time-series data for backtesting and factor research. Analyzing large DataFrames in pure Pandas can be memory-intensive and slow, while standing up a full PostgreSQL data warehouse for analytical workloads is operationally heavy.

## Decision
We chose DuckDB as the in-process OLAP database for all analytical and research workloads.

## Alternatives Considered
- SQLite: Too slow for column-oriented analytical queries.
- Pandas/Polars: Retained for in-memory manipulation, but DuckDB is used for SQL-based filtering, aggregations, and disk-spillover.
- PostgreSQL/ClickHouse: Too heavy for a single-node VPS deployment; requires external network hops.

## Consequences
- **Pros**: Blazing fast analytical queries on Parquet files, zero-dependency deployment, native integration with Pandas.
- **Cons**: DuckDB is not designed for concurrent heavy writes, limiting its use to read-heavy analytical workloads.

## Future Review Criteria
Re-evaluate if the dataset grows beyond a single disk (e.g., > 2TB) or if distributed compute (e.g., Spark, Ray) becomes necessary for multi-node backtesting.
