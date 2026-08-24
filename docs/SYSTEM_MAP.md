# System Map & Dependencies

## Core Dependency Flow
`Research -> Risk -> Execution -> Broker -> State Machine -> Reconciliation -> Audit`

## Module Map

- **`config/`**: Global settings and database connection singletons.
- **`ingestion/`**: Fetches market data (e.g., yfinance) and writes to immutable Parquet.
- **`data/`**: Abstractions for DuckDB analytics and Parquet storage.
- **`models/`**: Domain entities and Supabase Repository patterns (Orders, Executions, Users, Sessions).
- **`research/`**: Factor modeling, hypothesis generation, and CP-CV validation.
- **`backtest/`**: VectorBT PRO wrappers for vector-based strategy evaluation.
- **`portfolio/`**: Allocation and optimization algorithms (e.g., Equal Weight, Risk Parity).
- **`risk_kill/`**: The ultimate gatekeeper. Global kill-switches and exposure limits.
- **`execution/`**: Order state machines, paper trading engines, and broker adapters.
- **`auth/`**: OAuth flows, Fernet encryption, and session management.
- **`reconciliation/`**: Compares internal DB state with Broker state; triggers kill-switches on drift.
- **`observability/`**: Logging, health checks, and metrics.
- **`migrations/`**: SQL files to define the Supabase schema and RLS policies.
- **`cli/`**: Command-line interfaces for operating the system.
