# AUDIT VERIFICATION

Audit date: 2026-09-01 · Base commit `80490c3` · Working branch `arena/01a05c8e-indian-trading`

This document records **how each fix in this change set was verified**, what was *not*
verified, and how to reproduce every claim. Nothing here is asserted on the strength of a
green tick alone.

---

## 1. Environment

```
python 3.11.2 (system) → .venv (python -m venv .venv)
pip install numpy pandas pyarrow pytest duckdb pydantic pandera
pip install -e .        # yfinance, duckdb, supabase, vectorbt, scikit-learn,
                        # scipy, matplotlib, requests/httpx, cryptography, pyarrow
pip install seaborn pytest-mock hypothesis pytest-asyncio pytest-timeout ruff
```

`seaborn`, `pytest-mock`, `hypothesis` and `pytest-asyncio` had to be added manually — which is
itself finding AUDIT-017 (they are imported by the test suite but not declared).

Installed versions: pandas 2.3.3 (downgraded from 3.0.5 by the resolver), pydantic 2.13.5,
pandera 0.33.0, vectorbt 0.28.5, scikit-learn 1.9.0, scipy 1.17.1, cryptography 50.0.1,
pyarrow 25.0.1, supabase 2.31.0, pytest 9.1.1.

---

## 2. Test evidence

### 2.1 Before (commit `80490c3`, clean tree)

```
$ pytest tests/ -q
FAILED tests/test_real_data_pipeline.py::test_adapter_missing_field_raises
FAILED tests/test_real_data_pipeline.py::test_deterministic_snapshot
ERROR  tests/test_real_data_pipeline.py::test_universe_constituent_validation
ERROR  tests/test_real_data_pipeline.py::test_missing_constituent_handling
ERROR  tests/test_real_data_pipeline.py::test_universe_version_fingerprinting
ERROR  tests/test_real_data_pipeline.py::test_adjustment_state_validation
ERROR  tests/test_real_data_pipeline.py::test_symbol_continuity_isin
ERROR  tests/test_real_data_pipeline.py::test_ingestion_to_research_integration
ERROR  tests/test_real_data_pipeline.py::test_full_real_data_pipeline_ledger_and_reproducibility
2 failed, 1250 passed, 5 skipped, 7 errors, 12 warnings in 145.49s
```

### 2.2 After

```
$ git checkout -- data/quant.duckdb data/snapshots/test_snap.parquet   # clean state
$ pytest tests/ -q --timeout=300
1285 passed, 5 skipped, 3 xfailed, 12 warnings in 194.02s
```

Every failure and fixture error at HEAD is resolved. The delta is not a clean subtraction
because the collected count itself moves with the committed database (AUDIT-027): from
`2 failed, 7 errors, 1250 passed, 5 skipped` (1264 collected) to
`0 failed, 0 errors, 1285 passed, 5 skipped, 3 xfailed` (1293 collected). The substantive
changes are: **+26 new regression tests**, **+21 real-data tests that now actually execute**
(they previously errored at fixture setup), **−7 fixture errors**, **−2 failures**, and
**+3 strict xfails** that document AUDIT-014.

### 2.3 CI lint steps

```
$ ruff check dashboard scripts tests/test_operational_dashboard.py \
      tests/test_release_backup.py tests/test_release_dry_run.py
All checks passed!                     # was: Found 10 errors

$ ruff format --check <same paths>
50 files already formatted             # was: 2 files would be reformatted
```

### 2.4 New regression module

```
$ pytest tests/test_forensic_audit_regressions.py -q
26 passed in 3.70s
```

| Test | Guards |
| --- | --- |
| `test_audit_001_risk_state_has_a_health_mapping` (×5 states) | AUDIT-001 |
| `test_audit_001_unknown_risk_state_fails_closed` | AUDIT-001 |
| `test_audit_001_execution_halts_without_raising` | AUDIT-001 (end-to-end) |
| `test_audit_002_no_api_key_literal_in_scripts` | AUDIT-002 |
| `test_audit_002_macro_script_reads_the_key_from_the_environment` | AUDIT-002 |
| `test_audit_003_secret_scanner_catches_prefixed_names` | AUDIT-003 |
| `test_audit_004_ingest_cli_accepts_universe_dir` | AUDIT-004 |
| `test_audit_004_run_experiment_resolves_universe_root` | AUDIT-004 |
| `test_audit_004_membership_audit_merges_to_report_shape` | AUDIT-004 |
| `test_audit_004_universe_dataset_from_dir_descends` | AUDIT-004 |
| `test_audit_005_truncated_header_is_a_data_quality_error` | AUDIT-005 |
| `test_audit_005_both_upstream_header_dialects_parse` | AUDIT-005 |
| `test_audit_006_validate_market_bars_rejects_future_bars` | AUDIT-006 |
| `test_audit_006_pipeline_defaults_as_of_to_today_not_frame_max` | AUDIT-006 |
| `test_audit_007_universe_history_is_required_but_unused` | AUDIT-007 (characterisation) |
| `test_audit_014_panel_backfills_prices_before_listing` | AUDIT-014 (characterisation, 3 modes) |
| `test_audit_016_wheel_contains_the_data_package` | AUDIT-016 |
| `test_audit_017_seaborn_is_declared` | AUDIT-017 |
| `test_audit_018_dockerfile_installs_dependencies` | AUDIT-018 |
| `test_audit_019_compose_var_is_writable` | AUDIT-019 |
| `test_audit_020_preflight_runs_the_environment_policy` | AUDIT-020 |
| `test_audit_020_preflight_rejects_live_broker_credentials` | AUDIT-020 |

---

## 3. Direct execution evidence (not test-mediated)

| Finding | How it was reproduced | Output |
| --- | --- | --- |
| AUDIT-001 (defect) | `/tmp/repro1.py`: stale-data context → `ExecutionService.execute_targets` | `EXCEPTION: AttributeError HALTED` |
| AUDIT-001 (fix) | same call, post-fix | `ExecutionSummary(run_id='audit-001', risk_state='STOP_NEW_ORDERS', submitted=[], skipped=[], halted=True)` |
| AUDIT-003 (defect) | applied the committed regex to the committed line | `MATCH: None` |
| AUDIT-003 (fix) | applied the new regex to the same line, plus 5 control cases | `ALL OK` |
| AUDIT-010 | `validate_market_bars` over 20 committed clean symbols | `issues by kind: {'staleness': 1, 'missing_candle': 75, 'off_calendar': 61}` / `accepted 11504` |
| AUDIT-011 | weekday histogram of the off-calendar issues | `{'Saturday': 50, 'Sunday': 11}` incl. 2025-02-01, 2026-02-01 |
| AUDIT-012 | series histogram over 600 committed files | `EQ 1145575, BE 86775, SM 51847, ST 10175, BZ 2523`; 377/600 symbols affected |
| AUDIT-013 | live HTTP against the running server | `{"error": "strategy is not paper-approved by the research gate"}` |
| AUDIT-015 | offline `preview_rebalance` | `ready: False reason: configure UPSTOX_ACCESS_TOKEN; …` |
| AUDIT-014 | probe over the repository's own fixture world | `NEWCO close rows before listing: 306, NaNs: 0`, `head(3) = [121.18, 121.18, 121.18]` |
| AUDIT-007 | two `engine.run` calls with different `universe_history` | return series identical (`assert_series_equal` passes) |
| AUDIT-008 | 30 assets × 2 600 days, 3 cost settings × 2 backends | see `E2E_TRACE.md` §Trace 11 |
| AUDIT-016 | `pip wheel .` + `zipfile.namelist()` | `data/ in wheel: False`, `datahub/ in wheel: True` |
| AUDIT-018 | blocked third-party imports, then `get_paper_service()` / `_dispatch_api` | `ImportError BLOCKED third-party import: numpy` (×3) |
| AUDIT-020 | `python scripts/preflight.py` with `UPSTOX_API_KEY` set | exit 1, `ConfigurationError: Live broker credentials detected …` |
| No-live-order-path | `grep -rn "requests\|httpx\|urllib\|socket\|urlopen"` over broker/paper/dashboard/datahub | only `urllib.parse.unquote` in `dashboard/server.py` |
| Look-ahead hypothesis (**disproved**) | daily-rebalance "oracle" long on up-days, both backends | Sharpe −0.310 for both; buy-and-hold −0.762 ⇒ no same-bar fill |

---

## 4. What was NOT verified

Stated plainly, because an audit is only as good as its declared limits:

1. **The Docker image was never built.** Docker is not available in this sandbox. AUDIT-018 and
   AUDIT-019 were reproduced by import-blocking and by reading the mount flags respectively,
   not by starting a container. The fixes (install dependencies; writable `var/`) are
   one-line changes but are **unverified end-to-end**. Run `docker compose up` on a machine
   with Docker before trusting them.
2. **No real broker was contacted and no order was placed.** This was a hard constraint of the
   engagement and was respected. The conclusion "there is no code path to a broker order API"
   is based on static analysis (grep over every network-capable primitive in the relevant
   packages) plus reading `broker/transport.py`, not on observing a refusal from a broker.
3. **`datahub`/Upstox quote fetching was never exercised with a real token.** The SIM and EOD
   rungs of the quote chain work (observed live); the UPSTOX rung was not reachable.
4. **Supabase was never connected.** The production repositories (`execution/repositories.py`,
   `store/supabase.py`) are exercised only through `store/memory.py` in the test suite. Their
   behaviour against a real Supabase instance — including the `claim_run` concurrency guarantee
   and RLS — is unverified.
5. **No performance/load testing.** The 133-symbol clean bundle is small; behaviour at
   3 694 symbols (the full eod2 mirror) is inferred from the measured O(symbols × dates)
   behaviour of `detect_missing_candles`, not measured.
6. **Static analysis beyond ruff was not run.** `bandit` and `pip-audit` are in CI; I did not
   run them locally, so I make no claim about their output.
7. **The Streamlit dashboards were not started.** AUDIT-033 is based on the missing dependency,
   not on an observed failure (though it is a direct consequence of it).
8. **Numerical claims about the published MomReM card were not re-derived.** The README already
   documents a known Sharpe discrepancy (published 0.966 vs recomputed ~0.63) attributed to a
   rebalance-grid bug. I did not re-run the full MomReM experiment; AUDIT-014 and AUDIT-012 are
   *additional* defects that would affect it, not a re-derivation.

---

## 5. How to reproduce

```bash
git clone <repo> && cd indian_trading
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pip install seaborn pytest-mock hypothesis pytest-asyncio pytest-timeout ruff

# baseline (at commit 80490c3)
.venv/bin/python -m pytest tests/ -q --timeout=300      # 2 failed, 7 errors

# after this change set
.venv/bin/python -m pytest tests/ -q --timeout=300      # 1403 passed, 3 xfailed
.venv/bin/python -m pytest tests/test_forensic_audit_regressions.py -q
.venv/bin/python -m pytest tests/test_real_data_pipeline.py -q

.venv/bin/ruff check dashboard scripts tests/test_operational_dashboard.py \
    tests/test_release_backup.py tests/test_release_dry_run.py
.venv/bin/ruff format --check <same paths>

.venv/bin/python scripts/preflight.py            # fails closed; try with
UPSTOX_API_KEY=x .venv/bin/python scripts/preflight.py --json   # exit 1

# dashboard (manual UI verification, port 8080)
.venv/bin/python dashboard/server.py
curl -s localhost:8080/api/operations | head
curl -s -X POST localhost:8080/api/paper/preview \
     -H 'Content-Type: application/json' -d '{"strategy_id":"momrem"}'
```

---

## 6. Change inventory

Files changed (all on `arena/01a05c8e-indian-trading`):

| File | Nature |
| --- | --- |
| `risk_kill/mapping.py` | **new** — RiskState → SystemHealth mapping (stdlib-only, total, fail-closed) |
| `execution/service.py` | use the mapping; remove the non-existent enum members |
| `scripts/ingest_macro.py` | credential from the environment |
| `tests/test_architecture.py` | secret-scanner regex + value heuristic |
| `scripts/ingest_real_data.py` | `--universe-dir` alias; `build_parser()`; skip absent indices; `merge_membership_audit()` |
| `scripts/run_real_data_experiment.py` | `_resolve_universe_dir()` |
| `data/universe.py` | `from_dir` descends one level as a **fallback** |
| `ingestion/eod2_adapter.py` | header validation |
| `data/quality.py` | `as_of` threaded through `validate_market_bars` |
| `orchestration/pipeline.py` | `run_day(as_of=…)` defaulting to today in IST; `IST`/`_ist_today()` |
| `research/realdata.py` | `exclude_incomplete` / `fill_missing_prices`; `incomplete_symbols`; `price_fill` |
| `paper_trading/service.py` | quote chain in `preview_rebalance`; per-fill source labelling; `quote_sources` |
| `pyproject.toml` | `data` package; `seaborn` dependency |
| `Dockerfile` | install dependencies |
| `docker-compose.yml` | writable `var/`; HOME tmpfs |
| `scripts/preflight.py` | **new** |
| `tests/test_real_data_pipeline.py` | 2 path fixes (canonical PIT layout); 3 `xfail(strict=True)` |
| `tests/test_forensic_audit_regressions.py` | **new** — 26 tests |
| `dashboard/`, `scripts/`, 3 test files | ruff fix + format |
| `FORENSIC_AUDIT_REPORT.md`, `CRITICAL_FINDINGS.md`, `E2E_TRACE.md`, `SYSTEM_STATE_MACHINE.md`, `TEST_GAP_ANALYSIS.md`, `FIX_PLAN.md`, `AUDIT_VERIFICATION.md` | **new** — deliverables |

**Not changed (deliberately):** every safety control listed in `FIX_PLAN.md` Part B; every
default that would move a published research number; `config/paper_strategies.json`
(approving a strategy is the owner's decision, not an auditor's).

**Two tracked binary/data files are dirtied by running the test suite**
(`data/quant.duckdb`, `data/snapshots/test_snap.parquet`) — AUDIT-027. They were reverted
before this change set was finalised, but **any** subsequent `pytest tests/` run will dirty
them again until AUDIT-027 is fixed.
