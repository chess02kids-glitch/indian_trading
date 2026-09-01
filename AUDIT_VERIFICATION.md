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

### 2.2 After part A

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
7. **The Streamlit dashboards were not started.** AUDIT-033 was originally based on the missing
   dependency rather than an observed failure. `streamlit` is now declared as the optional
   `[dashboards]` extra, and `dashboard/streamlit_guard.py` was verified by importing
   `dashboard.main_dashboard` *without* streamlit installed and observing the actionable
   `RuntimeError` on first render — but no dashboard was ever started *with* streamlit
   installed, because the package is not installed in this sandbox.
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

**Fixed in part B (was: "two tracked binary/data files are dirtied by running the test
suite")** — AUDIT-027. See §7.6; the last full run left `git status` clean apart from the
intentional source edits.

---

## 7. Part B — remediation verification

The user's instruction after the audit was **"fix them all"**. This section records what each
Part B item changed, what it was verified against, and — because several items change safety
policy or published numbers — the **behavioural change** each one introduces.

Final state after all of Part B:

```
$ .venv/bin/python -m pytest tests/ -q --timeout=300 --timeout-method=thread
1385 passed, 5 skipped, 12 warnings in 209.53s

$ .venv/bin/ruff check .
All checks passed!            # `make lint` now works (see §7.11)

$ .venv/bin/ruff format --check dashboard scripts tests/test_operational_dashboard.py \
      tests/test_release_backup.py tests/test_release_dry_run.py
51 files already formatted

$ git status --short      # after the full run above
 M <source edits only>    # data/quant.duckdb and data/snapshots/ are CLEAN (AUDIT-027)
```

Test count moved `1290 → 1385` (+95 new tests: 39 dashboard-auth, 29 order-state-machine,
48 → 63 in the forensic regression module, plus the ones below).

### 7.1 B1 — kill switch is authoritative (AUDIT-021)

`datahub/kill_switch.py` is the only module that reads the switch for an authorisation
decision; `ExecutionService.execute_targets` and `DailyPipeline.run_day` consult it; the
switch is persisted, survives restart, and **fails closed** (an unreadable or corrupt state
file means `is_killed() is True`). The pipeline records and restores the automatic protective
state (`heartbeats.risk_state`) across a restart, and `scripts/run_daily.py` exits **21** on
an armed switch.

*Behavioural change:* an orchestrated run with the kill switch armed now stops submitting
orders. Before, ARM only stopped the paper page.

### 7.2 B2 — real risk-context inputs (AUDIT-030)

`broker_connected` is no longer hard-coded `True`; `RiskContext` is built from the live
broker `ping()`, real positions and real data timestamps.

*Behavioural change:* a run with an unreachable broker now halts instead of proceeding.

### 7.3 B3 — no fabricated pre-listing prices (AUDIT-014)

`research.realdata.build_market_panels` defaults flipped to
`exclude_incomplete=True, fill_missing_prices=False`; call sites pass them explicitly.

*Behavioural change — **every published Sharpe/CAGR/drawdown derived from a panel that
contained an incomplete symbol must be re-derived**.* The three `xfail(strict=True)` markers
that pinned the old behaviour were removed, and the characterisation test
`test_audit_014_panel_no_longer_invents_pre_listing_prices` now pins the new one (NEWCO is
excluded; `price_fill == "none_excluded"`).

### 7.4 B4 — `universe_history` is applied, not just required (AUDIT-007)

`backtest/engine.py` masks **weights** (`weights.where(membership, 0.0)`), reports
`metadata["membership_coverage"]`, and refuses `None`/`[]` (and every call site now passes
the PIT membership). Price-implied membership is an explicit opt-in
(`MEMBERSHIP_FROM_PRICES`).

Verified: `test_audit_007_universe_history_is_applied_not_just_required` — two runs with
different memberships now produce different weights (they were identical before).

*Note:* `membership_coverage == 1.0` means **no protection was applied**; it is reported so
the reader can see that.

### 7.5 B5 — one execution model per backtest (AUDIT-008/009)

`BacktestConfig(report_backend="pandas", allow_price_fill=False)`. Both backends are still
computed for comparison; only `report_backend` produces the reported numbers, and the
absolute final-equity difference is logged as `backtest_backend_divergence` and surfaced in
metadata as `backend_divergence_final_equity`.

Measured on a 300×3 GBM panel: `backend=pandas`, `cross_checked=True`, divergence
`0.000761269`.

*Behavioural change:* the reported Sharpe/CAGR no longer depends on whether the optional
`vectorbt` import succeeded — the same input now gives the same published number on every
machine. Published numbers produced before this change may move.

`allow_price_fill=False`: a gap in prices raises `ResearchInputError` naming the gap count
(verified: a 10-row NaN block → *"prices contain 10 gaps…"*). `allow_price_fill=True`
restores the historical `ffill().bfill()` behaviour explicitly.

### 7.6 B11/AUDIT-027 — a test run no longer dirties the repository

Root cause: `config.settings.StorageConfig.data_dir` was a **dataclass field whose default
was evaluated once at import**, and `settings` is a module-level singleton — so the
`QUANT_DATA_DIR` set by the isolation fixture was ignored. Worse, `StorageManager` and
`DuckDBManager` bound `settings.storage.raw_dir` as a *default argument*, which Python also
evaluates at import.

Fixes:
* the conftest fixture now redirects `settings.storage.data_dir` directly (monkeypatch
  restores it correctly) **and** sets `QUANT_DATA_DIR`;
* `StorageManager`/`DuckDBManager` resolve their paths at construction time;
* `StorageConfig.rebind()` re-reads the environment for callers that need it.

An intermediate fix left `StorageManager().data_dir is None`, which would have broken
`save_historical_data`; `test_audit_027_storage_manager_default_is_usable` pins that.

Verified: after a full 1 385-test run, `git status --short` shows **no** change to
`data/quant.duckdb` or `data/snapshots/test_snap.parquet`.

### 7.7 B6 — blocking vs advisory data-quality kinds

`data/quality.py` now partitions issues; only blocking kinds reject rows. Verified by the
existing suite plus `tests/test_dataset_pipeline.py` (4 passed).

### 7.8 B7 — reconcile against the broker (AUDIT-022)

`reconciliation/engine.py` diffs against `broker.get_order_status(...)` (the broker's view),
skips intents with no persisted local result as never-submitted, and raises
`ReconciliationError` if the broker raises or returns `None` for an order the local ledger
believes was submitted.

*Behavioural change:* reconciliation can now **fail** where it previously passed silently.

### 7.9 B8 — NSE calendar and the EQ series filter (AUDIT-011/012)

Calendar committed at `data/calendar/nse_trading_calendar.json` (NSE CM, 59 holidays, now
6 special sessions, 2024–2026; `special_sessions` checked **before** `holidays`; weekday
fallback logs a warning and never raises).

**Re-measured after the fix, over all 3 694 committed source files** (`data/eod2/daily`,
2026-09-02) — the earlier figure came from a 600-file sample:

| series | rows | meaning |
| --- | ---: | --- |
| `EQ` (kept) | 6 664 240 | normal equity market |
| `BE` | 500 424 | trade-for-trade / odd-lot session |
| `SM` | 308 264 | institutional / negotiated block |
| `NAN` | 269 627 | index, VIX and g-sec files (no equity series at all) |
| `ST` | 62 225 | SME trade-for-trade |
| `BZ` | 34 775 | negotiated block |

**1 175 315 of 7 839 555 rows (14.99%) were not the equity series** and were previously
stitched into one "close" per symbol.

**741 of 3 694 files contain no EQ row at all** and are now refused with
`DataQualityError` instead of yielding an empty or misleading history: 566 `*_SME` names,
165 Nifty index / g-sec files, India VIX, 9 delisted/odd names. Before the filter those
index files were ingested **as if they were tradeable equities**.

Off-calendar detection, re-measured on 120 random symbols (189 674 EQ rows):

* with the weekday-only calendar (pre-fix): **731** rows flagged;
* with the real calendar as first shipped: **625** — still 186 false positives;
* after adding the special sessions verified below: **12**.

The 186 false positives were three dates on which the exchange genuinely traded and my
calendar did not know it. Verified on 2026-09-02:

| date | what it was | source |
| --- | --- | --- |
| 2024-01-20 (Sat) | special live session, intraday switchover to the DR site | NSE/BSE circular 2023-12-28; [Business Standard](https://www.business-standard.com/markets/stock-market-news/bse-nse-to-open-on-january-20-for-special-trading-session-check-details-124011900529_1.html) |
| 2024-05-18 (Sat) | special live session, DR switchover | NSE/BSE circular 2024-05-13; [Mint](https://www.livemint.com/market/stock-market-news/stock-market-news-bse-nse-are-open-for-trade-tomorrow-this-is-the-reason-11715935341321.html) |
| 2025-10-21 (Tue) | Diwali Laxmi Pujan — **closed for the regular session but a live one-hour Muhurat session was held**, so real prices exist | [NDTV Profit](https://www.ndtvprofit.com/markets/diwali-2025-stock-market-holidays-will-nse-bse-remain-closed-for-laxmi-pujan-and-balipratipada); [Upstox](https://upstox.com/news/market-news/latest-updates/trading-holiday-during-diwali-2025-nse-and-bse-to-remain-closed-check-dates-time-and-more/article-182990/) |

The 12 remaining flags are **real upstream defects**: the same three symbols (`cipla`,
`powergrid`, `hal`) carry rows on four published 2026 holidays (2026-01-15, 2026-05-01,
2026-05-28, 2026-06-26). That is 0.006% of the sample — a precise signal, not noise.

### 7.10 B9 — the research gate no longer passes on declarations (AUDIT-032/038)

`research/gate.py` gained `min_trade_count=30` and now requires:

* a declared `tested_variants` (else `trial_count_declared` **warn**; previously the deflated
  Sharpe silently used `benchmarks + placebos + 1` and overstated the probability);
* out-of-sample returns (`in_sample_evidence` **warn**; `metrics["evidence_kind"]` is
  `in_sample` or `out_of_sample`);
* walk-forward/CPCV validation (missing → **fail**, previously **warn**);
* `trade_events >= min_trade_count`.

Check count 8 → 9.

On the trade count: `metadata["trade_count"]` counts *periods with non-zero turnover*, which
is 0 for a strategy that keeps the same names, so the gate now uses
`max(turnover_events, rebalance_events)` and reports both (`trade_events`,
`rebalance_events`, `turnover_events`). Measured: a monthly strategy over 780 periods
reported `trade_count == 1` but 36 rebalance events — the raw metric would have failed a
genuinely trading strategy.

### 7.11 B10 — the paper gate says *why* it is closed (AUDIT-013/034)

* `config/paper_strategies.json` now contains `momrem` — the only strategy with a target
  builder — with `paper_approved: false` and an explicit refusal reason.
* `PaperTradingService.rebalance_blockers()` and `status()["rebalance_blocked_reason"]`
  publish the reason. Verified end-to-end (loopback root):
  * monitor stopped → *"paper monitor is stopped"*;
  * not approved → *"strategy 'momrem' is not paper-approved: …"*;
  * kill switch armed → *"kill switch is armed (audit test)"*;
  * approved with no builder → *"no paper target builder has been registered for 'zzz'"*;
  * approved + running → `[]`.
* `dashboard/operations.py::reconciliation` no longer reports *"no paper service available"*
  when a paper service **is** attached (the branch compared against a string that the code
  above never produced).

*Not done:* `momrem` is **not** approved. Approving a strategy is the owner's decision.

### 7.12 B12 — dashboard authentication (AUDIT-039)

Before: bound `0.0.0.0` with no authentication, authorisation, CSRF or origin check — any
host that could reach the port could disarm the kill switch with one `curl`.

Now, in order: loopback default → refusal to bind a routable address without
`QUANT_DASHBOARD_TOKEN` (`DashboardAccessError`, before any socket or poller starts) →
same-origin check on every mutating request → `X-Quant-Token` compared with
`hmac.compare_digest`.

`tests/test_dashboard_auth.py` (**39 + 5 = 44 tests**) pins all of it, including
`test_every_mutating_route_is_rejected_without_a_token` over all 12 routes and
`test_do_post_route_list_matches_this_module`, which fails if a new route is added without
being added to the test.

Honest limitation, now documented in `docs/local_paper_trading.md`: the SPA is served by the
same server, so with `QUANT_DASHBOARD_TOKEN_IN_UI=1` the token is handed to any client that
can load the page. The token is **not** an authentication boundary for browser users — it
stops unauthenticated scripts, scanners and CSRF. A real deployment needs an authenticating
reverse proxy in front of a loopback bind.

### 7.13 B11 — the remaining P2/P3 items

| ID | Fix | Evidence |
| --- | --- | --- |
| AUDIT-024 | `config/risk_policy.py` — one source of truth; `paper_trading.DEFAULT_RISK_POLICY` is derived from `risk_kill.RiskLimits` taking the **stricter** value where they disagreed | paper drawdown 15% → **10%**; position stays 15%; `test_audit_024_*` assert nothing is looser than the guard |
| AUDIT-025 | `execution/state_machine.py` implemented (was 0 bytes) and wired: `PaperBroker` routes every mutation through `OrderStateMachine` | 29 tests; verified that `_store(PENDING)` on a FILLED order raises `InvalidOrderTransition` **and leaves the ledger untouched** |
| AUDIT-026 | `main.py` no longer indexes `sys.argv` inside a default | `test_audit_026_*` |
| AUDIT-028 | `/healthz` imports every critical subsystem **and** probes `numpy/pandas/duckdb/pydantic/pyarrow` by name; returns **503** with the failure list when any is broken | `test_healthz_returns_503_when_a_dependency_is_broken` |
| AUDIT-029 | the data-quality staleness window (6 d) and the trading window (18 h) are defined next to each other in `config/risk_policy.py` and checked by `assert_quality_window_is_consistent()` | `test_audit_029_*` |
| AUDIT-031 | `execution/orders.jsonl` regenerated from `scripts/generate_sample_data.py`, which now **validates every record against `OrderResult`** | `test_audit_031_execution_sample_matches_order_result` |
| AUDIT-033 | `streamlit` declared as the optional `[dashboards]` extra; `dashboard/streamlit_guard.py` gives an actionable error; Makefile targets install the extra | import of `dashboard.main_dashboard` without streamlit → `RuntimeError: streamlit is not installed … pip install -e ".[dashboards]"` |
| AUDIT-036 | `SYSTEM_MODE` defaults to **LOCAL** (matching `.env.example`); `.env.example` and `docs/secrets_management.md` now warn that `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` / `DHAN_CLIENT_ID` are **fatal** to the validator | `test_audit_036_*` |
| AUDIT-037 | `docker-compose.yml`: `./data:/app/data` (was `:ro`) — `/api/data/rebuild-prices` writes `data/clean/prices.parquet` and DuckDB defaults to `data/quant.duckdb` | `test_audit_037_data_mount_is_writable` |

**AUDIT-036 behavioural change (stated, not silent):** `SYSTEM_MODE` previously defaulted to
`PAPER`, which requires `DATABASE_URL`, so a clean checkout that followed the documented
setup **failed its own preflight**. It now defaults to `LOCAL` — the least-privileged mode,
which reaches neither a remote database nor a broker — and an unset mode is logged loudly.

### 7.14 Lint gate (`make lint`) — was broken, now passes

`ruff check .` reported **133 pre-existing findings** in the trading code and **~100 more**
in vendored/experimental trees. `make lint` therefore failed on every run, which in practice
meant nobody ran it.

* the in-scope findings (32, in `research/`, `tests/`, `datahub/`, `diagnostics/` and two
  root scripts) were **fixed**, including a dead `BacktestEngine` construction in
  `diagnostics/protocol.py` that reveals the "zero-cost edge" check reads a *stored*
  `zero_cost_results` block rather than re-running the backtest;
* `data/membership`, `research_live`, `.agents` and `scratch` are excluded with a comment
  explaining that they are vendored/experimental and that cleaning them is separate work.

`ruff check .` → **All checks passed!**

### 7.15 Still NOT verified / still open

1. **Docker was never built** (unavailable in this sandbox). AUDIT-018/019/037 are fixed by
   reading and by targeted tests, not by starting a container.
2. **No real broker order, no real broker token.** Unchanged from §4.
3. **The FRED API key committed in the repository's history must be rotated by the owner.**
   Revoking the key is the only remedy; the file was removed but the secret is in the
   pushed history.
4. **The Streamlit dashboards were never started with streamlit installed** (not installed
   here); only the absence path is verified.
5. **The Indian charge table in `config/costs.py`** (`TABLE_VERSION =
   "india-charges-2026.08 (verify before production)"`) is still **not** checked against
   authoritative regulatory/broker sources. This is the largest remaining correctness gap.
6. **`config/paper_strategies.json` still has 31 strategies, all `paper_approved: false`.**
   The system is therefore still unable to place a paper order end-to-end by design.
