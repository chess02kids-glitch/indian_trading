# Repository Guide

The backend uses a standard Repository pattern to wrap the `supabase-python` REST client.
This abstracts all raw HTTP querying away from the logic layers.

## Features
- **Typed APIs:** Fully typed parameters.
- **Resilience:** All repository methods utilize the `@with_retries` decorator which implements exponential backoff.
- **Module Locality:** Repositories are located inside their relevant domain modules:
  - `portfolio/repositories.py` -> Orders, Positions
  - `execution/repositories.py` -> Executions
  - `reconciliation/repositories.py` -> Recon Logs
  - `research/repositories.py` -> MLFlow Experiments
  - `models/repositories.py` -> Users

The `get_supabase_client()` handles connection singletons via `DATABASE_URL` and `SUPABASE_KEY` env vars.
