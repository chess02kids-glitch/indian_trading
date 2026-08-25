# Repository Guide

The backend uses a standard Repository pattern with dependency inversion to separate persistence from domain logic. 
This abstracts all raw HTTP or database querying away from the logic layers.

## Boundary Ownership

Repository ownership is explicitly defined across the architecture to guarantee strict separation of concerns:

- **`models/`**: Owns the core immutable domain models (`ExecutionMode`, `OrderIntent`, `Position`, `RiskDecision`). It does *not* contain persistence logic.
- **`store/`**: Owns the database implementation (`store/supabase.py`) and protocol interfaces (`store/protocols.py`), providing concrete dependency injection for other layers.
- **`execution/`**: Owns order submission, paper execution simulations, and interacts with the `OrderRepositoryProtocol` and `PositionRepositoryProtocol`.
- **`portfolio/`**: Owns target generation, rebalancing logic, and backtest portfolio state.
- **`research/`**: Owns the immutable research ledger (`research/ledger.py`), hypothesis tracking, and experimental backtesting (DuckDB/Parquet datasets).
- **`reconciliation/`**: Owns the cross-check between expected portfolio state and actual execution state, utilizing `ReconciliationRepositoryProtocol`.

## Resilience
- All database interactions (in `store/supabase.py` or elsewhere) utilize the hardened `@with_retries` decorator which implements exponential backoff for transient failures, while strictly failing fast on idempotency violations (e.g., HTTP 409, 23505).

The `get_supabase_client()` handles connection singletons via `DATABASE_URL` and `SUPABASE_KEY` env vars.
