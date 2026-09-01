# TEST GAP ANALYSIS

Audit date: 2026-09-01 · Base commit `80490c3`

---

## 1. What the suite actually looks like

| Metric | At HEAD (`80490c3`) | After the audit |
| --- | --- | --- |
| Test modules | 80 | 81 (+ `tests/test_forensic_audit_regressions.py`) |
| Collected | 1264 | 1293 |
| Passed | 1250 | 1285 |
| Failed | 2 | **0** |
| Errors (fixture/setup) | 7 | **0** |
| Skipped | 5 | 5 |
| xfail (strict) | 0 | 3 (AUDIT-014) |
| Runtime | 145 s | 194 s |

All figures are **clean-state**: `data/quant.duckdb` and `data/snapshots/test_snap.parquet`
restored from git immediately before each run. The collected count is itself unstable — see
§2.5, AUDIT-027.

CI steps, run locally:

| Step | At HEAD | After |
| --- | --- | --- |
| `ruff check dashboard scripts tests/…` | **Found 10 errors** | All checks passed |
| `ruff format --check …` | **2 files would be reformatted** | 50 files already formatted |
| `pytest tests/ -m "not operational"` | 2 failed, 7 errors | 1285 passed, 5 skipped, 3 xfailed |

---

## 2. Structural gaps — tests that exist but prove the wrong thing

### 2.1 Nine real-data tests never executed

`tests/test_real_data_pipeline.py`'s module-scoped fixture called
`scripts/ingest_real_data.py` with `--universe-dir`, which the CLI did not define.
`argparse` exits 2, the fixture raises `SystemExit`, and **7 tests error at setup and 2 more
fail** as collateral. The suite reported green for 1258 tests while the only module that
exercises the real-data path — ingestion, PIT universe, completeness report,
ingestion→research integration, reproducibility — was dead.

This is the single most important structural gap: **the tests that would have caught
AUDIT-005, AUDIT-014 and the report-shape bug were themselves broken.**

Fixed. Progression: `2 failed, 7 errors, 13 passed` → `21 passed, 3 xfailed`.

### 2.2 A test documented behaviour that was never implemented

`test_adapter_missing_field_raises` asserted `pytest.raises(DataQualityError,
match="unexpected header")`. No header check existed in `ingestion/eod2_adapter.py`; the code
raised `AttributeError` instead. The test was a **specification, not a verification** — and it
had been failing silently in the noise of the fixture errors.

### 2.3 The secret scan had a false negative

`tests/test_architecture.py::TestNoSecretsInSource` passed while
`scripts/ingest_macro.py` contained a live FRED API key, because `\b` cannot match after `_`
in `FRED_API_KEY`. A security test that passes while the thing it guards against is present is
worse than no test.

### 2.4 Mocks substituted for external integrations

| Area | What is mocked | What is untested |
| --- | --- | --- |
| Broker | `SimulatedSandboxTransport` is the only transport; `HttpSandboxTransportStub` refuses to send | Real wire format, real auth failure modes, real partial fills |
| Upstox quotes | `QuoteChain` SIM rung | Real quote payload parsing, real rate limiting |
| Database | `store/memory.py` in-memory repos | Supabase `ExecutionsRepository`, RLS, `claim_run` under concurrency |
| Deployment | none | The Docker image was never built or started (AUDIT-018/019) |

The sandbox design is deliberate and defensible — but it means **the entire broker integration
is verified against a simulator that the same repository wrote**. Nothing validates the adapter
against a real broker contract.

### 2.5 Tests mutate committed data

`git status` after a plain `pytest tests/` shows:

```
 M data/quant.duckdb
 M data/snapshots/test_snap.parquet
```

Tests write into tracked, committed files. A test run is not reproducible against a clean
checkout, and有一天 the churn will be committed by accident. (AUDIT-027.)

---

## 3. Behavioural gaps — no test at all

These are the gaps that let the P0/P1 findings survive. Each is written as a test name plus the
assertion it must make, in the order I would implement them.

| # | Test | Guards | Assertion |
| --- | --- | --- | --- |
| G1 | `test_kill_switch_blocks_orchestrated_execution` | AUDIT-021 | With `datahub.state` armed, `DailyPipeline.run_day` returns `halted_kill_switch`, and `ExecutionService.execute_targets` returns `halted=True` with `submitted == []` |
| G2 | `test_kill_switch_blocks_every_order_creating_path` | AUDIT-021 | Parameterise over `execute_targets`, `run_day`, `PaperTradingService.execute_rebalance`, `run_automation_once` |
| G3 | `test_risk_state_survives_restart` | AUDIT-021 | After `LOCK_ACCOUNT`, a freshly constructed pipeline still refuses to trade |
| G4 | `test_pipeline_risk_context_uses_persisted_equity` | AUDIT-030 | `equity_day_start` differs from `equity_now` after a losing day; the daily-loss check trips |
| G5 | `test_pipeline_risk_context_uses_real_broker_heartbeat` | AUDIT-030 | With no heartbeat, `broker_connected` is `None`/`False` ⇒ `STOP_NEW_ORDERS` or `LOCK_ACCOUNT` |
| G6 | `test_pipeline_runs_on_real_multi_symbol_data` | AUDIT-010 | A 50-symbol slice of `data/clean/eod2_data` does **not** halt the day |
| G7 | `test_pipeline_halts_on_rejected_rows` | AUDIT-010 | A frame with real invalid rows still halts (the safety property must survive G6) |
| G8 | `test_backends_agree_on_zero_cost` | AUDIT-008 | `vectorbt` and `pandas` total returns agree to < 1e-6 with zero costs |
| G9 | `test_cost_drag_reconciles_with_equity` | AUDIT-008 | `sum(trades.total_cost)` equals gross-return minus net-return implied by the reported equity curve |
| G10 | `test_backtest_rejects_or_masks_price_gaps` | AUDIT-009 | A panel with a NaN block raises `ResearchInputError` or yields `NaN`, never a filled price |
| G11 | `test_universe_history_masks_the_cross_section` | AUDIT-007 | A symbol that is not a member on date *t* has weight 0 at *t* |
| G12 | `test_adapter_filters_non_eq_series` | AUDIT-012 | A file containing `BE` rows yields an EQ-only frame, with the drops reported |
| G13 | `test_calendar_knows_nse_holidays` | AUDIT-011 | `is_trading_day` is `False` on a known NSE holiday and `True` on a known special session |
| G14 | `test_reconciliation_detects_a_missing_fill` | AUDIT-022 | A fill known to the broker but not the local store ⇒ `locked=True` |
| G15 | `test_rebalance_blocked_when_not_paper_approved` | AUDIT-013 | `preview_rebalance` raises, and `status()` exposes why |
| G16 | `test_preview_uses_the_quote_chain` | AUDIT-015 | With no token, `preview` produces SIM-priced orders rather than failing |
| G17 | `test_fill_source_reflects_the_quote_source` | AUDIT-015 | A SIM-priced fill is stamped `sim_quote_read_only`, never `upstox_*` |
| G18 | `test_gate_fails_without_validation_evidence` | AUDIT-032 | `validation=None` ⇒ `FAIL`, not `FRAGILE` |
| G19 | `test_gate_dsr_counts_all_trials` | AUDIT-032 | `trials=100` lowers the DSR probability versus `trials=1` |
| G20 | `test_order_state_machine_rejects_illegal_transitions` | AUDIT-025 | `FILLED → PENDING` raises |
| G21 | `test_wheel_installs_and_imports` | AUDIT-016 | Build a wheel, install into a clean venv, `import data.quality` |
| G22 | `test_clean_install_collects_every_test` | AUDIT-017 | `pip install -e .[dev]` in a clean venv, then `pytest --co` exits 0 |
| G23 | `test_dashboard_healthz_reflects_dependencies` | AUDIT-028 | `/healthz` fails when the data layer cannot import |
| G24 | `test_tests_do_not_mutate_committed_data` | AUDIT-027 | `git status --porcelain` is empty after a full run |
| G25 | `test_env_validator_matches_env_example` | AUDIT-036 | Every variable in `.env.example` is accepted by `validate_environment` |

**G1–G5 and G8–G13 assert behaviour that does not currently exist.** Writing them means
changing a safety control or published research numbers, so they are specified here and
deliberately **not** committed as failing tests: the `FIX_PLAN.md` entries for those findings
must land first, and then the tests.

---

## 4. Coverage of the audit's required scenarios

| Scenario from the brief | Covered today? | Where / gap |
| --- | --- | --- |
| One trade (happy path) | Partial | `tests/test_orchestration.py` — 2 symbols only |
| One trade on real data | **No** | G6 |
| Bad trade (risk halt) | Partial | `tests/test_orchestration.py` halts *before* execution; the halt inside `execute_targets` had no test until `test_audit_001_execution_halts_without_raising` |
| Duplicate trade | Partial | idempotency is tested; reconciliation of duplicates is tautological (G14) |
| Stale data | Yes | `tests/test_data_quality.py`, plus AUDIT-006 regression |
| Token expiry | Yes | `tests/test_broker_token.py`, `tests/test_sandbox_execution.py` |
| Restart / DR | **No** | G3, G4 |
| Kill switch | Partial | paper path only; execution/orchestration untested (G1, G2) |
| Research gate | Yes, but loose | `tests/test_research_gate.py` — no test for the three leaks (G18, G19) |
| Backtest cost model | Yes | `tests/` cover `config/costs.py`; **no** test that both backends agree (G8) |
| Look-ahead | Yes for factors/constructors; **no** for the pipeline as-of guard | AUDIT-006 regression now covers it |
| Survivorship | **No** | G11 — the guard is unused |
| UI | **No automated test** | only manual HTTP verification (Trace 13) |
| Deployment | **No** | G21, G22; the image was never built |

---

## 5. What the passing suite does *not* tell you

At HEAD the suite was 1250/1264 green. That number concealed:

* 9 tests that never ran (the entire real-data module);
* 1 security test passing while its subject was leaking a credential;
* a fabricated-price defect in the anti-survivorship module;
* a mandatory-but-unused survivorship parameter;
* three inert risk checks;
* a kill switch that does not gate execution;
* a deployment that cannot start.

**A passing test suite is not evidence of a correct trading system.** The gaps above are
ordered by that principle: the highest-value missing tests are the ones that assert a
*protective* behaviour, not a happy path.
