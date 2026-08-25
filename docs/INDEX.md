# Repository Index

## Folder Purpose
- `agents/`: AI assistant plugins (not system runtime).
- `auth/`: Security, encryption, and broker sessions.
- `backtest/`: VectorBT execution pipelines.
- `cli/`: User interfaces and operators.
- `config/`: Environment loading and DB connections.
- `dashboard/`: (Future) UI for monitoring.
- `data/`: DuckDB and Parquet IO.
- `deploy/`: Systemd, bash, and Docker definitions.
- `docs/`: Architecture and runbooks.
- `execution/`: Order routing and state machines.
- `ingestion/`: Historical data fetchers.
- `migrations/`: Supabase SQL definitions.
- `models/`: DB access repositories.
- `observability/`: Logging and health.
- `orchestration/`: Workflow triggers.
- `portfolio/`: Weight allocation math.
- `reconciliation/`: Position auditing.
- `reports/`: Tiersheets and output generation.
- `research/`: Factor models and hypothesis generation.
- `risk_kill/`: Safety limits and halts.
- `tests/`: Pytest suite.

## Key Classes & Interfaces
- `OAuthFlow` (`auth/oauth.py`)
- `StorageManager` (`data/storage.py`)
- `DuckDBManager` (`data/duckdb_manager.py`)
- `FactorStrategy` (`research/factors.py`)
- `PortfolioConstructor` (`portfolio/construction.py`)
- `OrdersRepository` (`models/repositories.py`)

## CLI Command Index
- `quant-india auth upstox`
- `quant-india auth status`
- `quant-india auth validate`
- `quant-india research ...`
- `quant-india execution ...`

## Migration Index
- `001_initial_schema.sql`: Core tables (users, orders, executions, positions).
- `002_rls_policies.sql`: Row-level security for multi-tenant safety.
- `003_audit_log.sql`: Triggers for immutable ledger history.

- [Daily PAPER forward-testing loop](daily_forward_testing.md)
