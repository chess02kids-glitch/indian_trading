# Agent 2 Handoff — Supabase / Integration

**From:** Agent 1 (Quant India core build)
**Date:** 2026-08-24
**Branch:** `arena/01a0348f-indian-trading`
**Commit:** `3abd5e5` — `feat: build Quant India deterministic research and paper execution foundation`

---

## Completed

### Phase 1 — Core foundation ✅
- Pydantic execution-domain models in `models/domain.py`: `MarketBar`,
  `OrderIntent`, `OrderResult`, `Position`, `PortfolioTarget`,
  `RiskDecision`, `ReconciliationResult`, `ResearchResult`,
  `ExecutionMode` (RESEARCH/PAPER/SANDBOX — **no LIVE member by design**).
- **LIMIT-only hard invariant**: `OrderType` has exactly one member
  (`LIMIT`); `execution/validation.py::validate_order_intent` is the single
  deterministic choke point and rejects MARKET/IOC/invalid quantity, price,
  symbol, side, and future timestamps explicitly (never converts).
- Deterministic idempotency: `execution/idempotency.py` — stable
  `compute_idempotency_key(...)` + thread-safe `IdempotencyRegistry`;
  request → timeout → retry produces the same key and is rejected as a
  duplicate.
- `risk_kill/` rewritten as a deterministic, stdlib-only kill guard:
  daily loss, max drawdown, position/gross exposure, data staleness, broker
  connectivity, order rate, duplicate order, reconciliation lock; states
  `NOMINAL`, `ALERT_HUMAN`, `STOP_NEW_ORDERS`, `CANCEL_OPEN_ORDERS`,
  `FLATTEN_POSITIONS`, `LOCK_ACCOUNT`. Fails closed on unknown inputs.
- Repository interfaces in `store/protocols.py` (`OrderRepository`,
  `PositionRepository`, `RunRepository`, `ResearchRepository`,
  `ReconciliationRepository`) with in-memory (`store/memory.py`) and SQLite
  (`store/sqlite.py`) backends. `RunRepository.claim_run` is atomic
  (concurrent executions cannot duplicate a run).
- **This is where you plug in Supabase.**

### Phase 2 — Research + backtesting ✅
- Validated OHLCV handling in `data/quality.py` (row-level OHLC invariants,
  duplicates, timestamp validation, staleness, per-symbol missing candles;
  invalid rows reported, never silently filled).
- Quality factors (`models/quality.py`): `RoeQualityFactor`,
  `DebtQualityFactor`, `CompositeQualityFactor` on the existing
  `QualityFactor` interface.
- Configurable India cost model: `config/costs.py` (versioned,
  environment-overridable charge table: brokerage, STT buy/sell, exchange,
  SEBI, stamp duty, GST) + `backtest/costs.py::IndiaCostModel`
  (optimistic/base/pessimistic scenarios, per-charge breakdowns). The
  backtest engine accepts it as a drop-in for `research.contracts.CostModel`.
- Baseline strategy `MomentumQualityStrategy` (3M momentum × quality screen,
  Nifty 100 snapshot, long-only) in `research/strategies.py`.
- All five baselines (buy-and-hold, equal weight, inverse volatility,
  persistence, seeded random placebo) already exist in
  `backtest/benchmarks.py` and run under the same cost model.
- Metrics extended: Sortino, win rate, trade count, cost drag
  (`backtest/metrics.py`).
- Research ledger `research/ledger.py`: `HYP-00001...` sequencing,
  append-only JSONL, **rejected experiments recorded too**.
  `Experiment`/`ExperimentRecord` now carry dataset version, cost model,
  backtest/OOS periods (MLflow in `research/experiments.py`).
- End-to-end deterministic experiment: `scripts/run_research_experiment.py`
  (run it: `python scripts/run_research_experiment.py`).
- Pinned determinism snapshot: `tests/test_backtest_determinism.py`.

### Phase 3 — Paper execution + integration contract ✅
- `execution/adapter.py`: `ExecutionAdapter` protocol (submit/cancel/status/
  positions/open orders).
- `execution/paper.py`: deterministic `PaperBroker` (full/partial fills,
  rejections, cancellations, TTL expiry, duplicate detection; seeded RNG +
  injected clock; **no network, no credentials, cannot touch live capital**).
- `execution/service.py`: `ExecutionService` — targets → intents → LIMIT
  validation → risk gate → idempotency → adapter. Halts on any protective
  risk state (fail closed).
- `reconciliation/engine.py`: `ReconciliationEngine` — expected state is
  derived from the **persisted order ledger** (never from the broker), so
  broker drift is detectable; any mismatch → `LOCK_ACCOUNT` until a human
  resolves (ADR-008).
- `orchestration/pipeline.py`: `DailyPipeline` — data validation → research
  → allocation → risk → **human approval gate (fail closed)** → paper
  execution → fill reconciliation → EOD reconciliation → health status.
- `observability/health.py` (`HEALTHY/WARNING/HALTED/LOCKED`, monotonic,
  operator reset, JSON status doc) and `observability/alerts.py`
  (`INFO/WARNING/CRITICAL`; Telegram only via `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` environment variables — no other module contains
  Telegram logic).
- `dashboard/paper_dashboard.py`: Streamlit foundation
  (`streamlit run dashboard/paper_dashboard.py`).
- Architecture boundary tests in `tests/test_architecture.py` (run in CI):
  risk_kill import closure contains no AI modules; execution path has no
  network/broker coupling; no hardcoded credentials.

### Final test suite (12 safety tests) — all passing
1. Risk package cannot import AI — `tests/test_architecture.py::TestRiskKillBoundary`
2. Market order rejected — `tests/test_order_validation.py::TestLimitOnlyInvariant`
3. IOC rejected — same class
4. Duplicate order rejected — `tests/test_idempotency.py` + `tests/test_paper_execution.py::TestDuplicates`
5. Daily loss triggers stop — `tests/test_risk_kill.py::TestDailyLoss`
6. Drawdown triggers flatten — `tests/test_risk_kill.py::TestDrawdown`
7. Stale data prevents signal generation — `tests/test_orchestration.py::TestDailyFlow::test_stale_data_prevents_signal_generation`
8. Partial fill updates position — `tests/test_paper_execution.py::TestFills::test_partial_fill_updates_position`
9. Reconciliation mismatch locks account — `tests/test_reconciliation.py` + `tests/test_orchestration.py::TestDailyFlow::test_broker_drift_locks_account`
10. Concurrent execution cannot duplicate the run — `tests/test_store.py::TestRunRepository::test_concurrent_claim_single_winner` + `tests/test_orchestration.py::TestDailyFlow::test_concurrent_runs_cannot_duplicate`
11. Backtest is deterministic — `tests/test_backtest_determinism.py`
12. Malformed market data rejected — `tests/test_data_quality.py::TestOhlcvValidation::test_final_suite_malformed_ohlc_rejected`

**Full suite: 400 passed, 0 failed.**

---

## Interfaces Agent 2 can integrate with

Implement Supabase adapters against these protocols — the core depends only
on the protocols, so no domain code changes are needed:

| Interface | Module | Notes |
| --- | --- | --- |
| `OrderRepository` | `store/protocols.py` | `save_intent`, `get_intent`, `save_result`, `get_result`, `find_by_idempotency_key`, `list_intents` |
| `PositionRepository` | `store/protocols.py` | `upsert_position`, `get_position`, `list_positions` |
| `RunRepository` | `store/protocols.py` | `claim_run` **must remain atomic/exclusive**; `save_run`, `get_run`, `list_runs` |
| `ResearchRepository` | `store/protocols.py` | `save_result`, `latest_result`, `list_by_hypothesis` |
| `ReconciliationRepository` | `store/protocols.py` | `save_result`, `latest_result(run_id=None)`, `list_results` |

Reference behaviour to match exactly: `store/memory.py` and `store/sqlite.py`
(semantics, thread-safety, atomic claim).

Other integration points:

- **Execution adapter** — `execution/adapter.py::ExecutionAdapter`. A future
  live adapter is a *separate, human-approved project*; it must validate
  every order with `execution.validation.validate_order_intent`, pass the
  risk guard, and register idempotency keys.
- **Health status document** — `var/operational_status.json`
  (override with `QUANT_INDIA_PAPER_STATUS`); read by
  `dashboard/paper_dashboard.py` and `dashboard/operational.py`
  (`QUANT_INDIA_STATUS_FILE`).
- **Alerts** — `observability/alerts.py::AlertService`; credentials only
  from `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` env vars.
- **Research ledger** — `research/ledger.py::HypothesisLedger`
  (JSONL at `reports/generated/experiments/ledger.jsonl`); MLflow local store
  under `reports/generated/experiments/mlflow.db` (file store — you may move
  the tracking URI; do not change the record schema without updating
  `tests/test_experiments_reporting_cli.py`).
- **Existing Supabase clients** (pre-existing, Agent-2-owned):
  `config/database.py`, `portfolio/repositories.py`,
  `research/repositories.py`, `execution/repositories.py`,
  `reconciliation/repositories.py`, `models/repositories.py`,
  `models/sessions.py`, `auth/*`.

## Files Agent 2 is allowed to modify

- `config/database.py` (connection/retry plumbing) — **do not** loosen retry
  semantics without updating `tests/test_database.py`
- `portfolio/repositories.py`, `research/repositories.py`,
  `execution/repositories.py`, `reconciliation/repositories.py`,
  `models/repositories.py`, `models/sessions.py`
- `auth/*`
- `migrations/*`, `scripts/verify_migrations.py`, `scripts/backup.py`
- `deploy/*`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`
  (CI wiring only — **do not remove the `tests/test_architecture.py` or
  bandit steps**)
- `dashboard/operational.py`, `dashboard/server.py`,
  `dashboard/paper_dashboard.py`
- New Supabase adapter modules (e.g. `store/supabase.py`)
- `requirements/*.txt`, `pyproject.toml` (dependency management)
- `.env.example` (add Supabase/Telegram placeholders — **placeholders
  only, never real values**)

## Files Agent 2 should NOT modify (without explicit human approval)

- `risk_kill/*` — the AI-independent kill switch (CI enforces zero
  non-stdlib imports)
- `models/domain.py` — core domain models (schema changes are architecture
  changes)
- `execution/validation.py`, `execution/idempotency.py`,
  `execution/paper.py`, `execution/service.py`, `execution/adapter.py`
- `reconciliation/engine.py`
- `research/contracts.py`, `research/factors.py`, `research/strategies.py`,
  `research/runner.py`, `research/experiments.py`, `research/ledger.py`
- `backtest/*` (engine, metrics, costs, benchmarks, validation)
- `portfolio/construction.py`
- `data/quality.py`, `models/quality.py`
- `orchestration/pipeline.py`
- **Existing tests** — especially `tests/test_architecture.py`,
  `tests/test_order_validation.py`, `tests/test_risk_kill.py`,
  `tests/test_backtest_determinism.py` (pinned snapshot),
  `tests/test_reconciliation.py`, `tests/test_orchestration.py`

If one of these must change: open a discussion with the human operator
first; the boundary tests exist to keep the safety invariants honest.

## Supabase integration requirements (concrete)

1. Implement the five `store/protocols.py` interfaces against the existing
   tables in `migrations/001_initial_schema.sql`
   (`orders`, `positions`, `executions`, `reconciliation_log`,
   `experiments`) — or extend the schema with **new** migrations
   (`004_...`, never edit `001`–`003`).
2. `RunRepository.claim_run` semantics: exactly one writer wins per run id
   (unique constraint / `ON CONFLICT DO NOTHING` + rowcount).
3. Idempotency: `orders.idempotency_key` must be unique; lookups must match
   `InMemoryOrderRepository.find_by_idempotency_key` semantics.
4. Keep the existing RLS policies (`migrations/002_rls_policies.sql`) intact;
   add policies for any new tables.
5. Verification: `python scripts/verify_migrations.py` must stay green.
6. Do **not** put secrets in the repo; env vars only (see `.env.example`).
7. Do **not** wire any adapter to a live broker — there is no live broker
   mode in this system.

### Dependency note (important)

`mlflow` is an **optional extra** (`.[tracking]`), not a base dependency:
every mlflow release caps `cryptography <50` or `pyarrow <23`, while the
safe ranges for PYSEC-2026-3552 (fixed 50.0.0) and PYSEC-2026-113 (fixed
23.0.1) are `cryptography >=50.0.0` and `pyarrow >=23.0.1`. Base deps pin
the safe ranges so CI's `pip-audit --strict` passes. `research/experiments.py`
degrades to the local JSONL audit trail when mlflow is absent, so nothing
breaks without the extra. If a future mlflow release lifts the caps, the
extra can be merged back into base deps.

## Operating rules (unchanged)

- All execution stays in RESEARCH / PAPER / SANDBOX. No live capital.
- LIMIT orders only. Human approval gate stays fail closed.
- Unknown ≠ safe. The system halts on unknown safety-relevant inputs.
