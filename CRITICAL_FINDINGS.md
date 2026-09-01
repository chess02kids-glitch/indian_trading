# CRITICAL FINDINGS — Quant India

Audit date: 2026-09-01 · Repository: `chess02kids-glitch/indian_trading` · Base commit: `80490c3` ("bugs")
Auditor stance: nothing is assumed to work. Every finding below was reproduced by executing
code in this repository, not inferred from names, docstrings, or tests.

Severity scale (per audit brief):

| Sev | Means |
| --- | --- |
| **P0** | Unintended live orders, bypassed risk/kill-switch, corrupted state, severe loss, invalidated backtests, credential leak, unknown live positions |
| **P1** | A safety or correctness control that is present but non-functional, or a workflow that cannot complete |
| **P2** | Correctness/ops defect with a workaround, or a control that is weaker than documented |
| **P3** | Hygiene, dead code, misleading artefacts |
| **P4** | Cosmetic |

Every finding uses the required template. IDs are stable and referenced from
`FORENSIC_AUDIT_REPORT.md`, `FIX_PLAN.md`, `E2E_TRACE.md`, `TEST_GAP_ANALYSIS.md`
and `AUDIT_VERIFICATION.md`.

---

## Summary table

| ID | Sev | Category | One line | Status |
| --- | --- | --- | --- | --- |
| AUDIT-001 | **P0** | Risk / execution | Every protective risk decision crashed `ExecutionService` with `AttributeError` instead of halting | **FIXED** |
| AUDIT-002 | **P0** | Security | A live FRED API key was committed to `scripts/ingest_macro.py` | **FIXED** (rotate!) |
| AUDIT-003 | **P0** | Security | The secret scanner's `\b` anchor made `FRED_API_KEY = "…"` invisible to it | **FIXED** |
| AUDIT-014 | **P0** | Data integrity | The research panel invents pre-listing prices and reports the symbol as "excluded" | **FIXED** — defaults flipped (B3); every published number from a panel with an incomplete symbol must be re-derived |
| AUDIT-021 | **P0** | Kill switch | The operator kill switch is not consulted by execution or orchestration; the deterministic guard has no persistence | **FIXED** — `datahub/kill_switch.py` is the sole authority; consulted by execution and orchestration; fails closed |
| AUDIT-039 | **P0** | Security | The dashboard has no authentication at all, and an unauthenticated POST can **disarm** the kill switch | **FIXED** — loopback default, refusal on a routable bind without a token, origin + `X-Quant-Token` on all 12 mutating routes |
| AUDIT-030 | **P0** | Risk | 3 of the risk checks (daily loss, broker connectivity, order rate) are structurally dead in the orchestrated path | **FIXED** — risk context is built from real broker ping/positions/timestamps |
| AUDIT-004 | P1 | Real-data pipeline | The real-data CLI rejects the argument its own tests pass; 9 tests were dead | **FIXED** |
| AUDIT-005 | P1 | Ingestion | A malformed source header crashes the adapter instead of raising `DataQualityError` | **FIXED** |
| AUDIT-006 | P1 | Look-ahead | `validate_market_bars` never forwarded `as_of`, so the future-date guard was dead | **FIXED** |
| AUDIT-007 | P1 | Survivorship | `universe_history` is mandatory but never read — the guard is decorative | **FIXED** — the membership mask is applied to weights; `membership_coverage` reported |
| AUDIT-008 | P1 | Backtest | Returns come from VectorBT while turnover/cost/trade-count come from pandas | **FIXED** — `BacktestConfig.report_backend`; both backends computed, one reported, divergence logged |
| AUDIT-009 | P1 | Backtest | `_validate_inputs` silently ffill+bfills the price panel | **FIXED** — `allow_price_fill=False`; a gap raises `ResearchInputError` |
| AUDIT-010 | P1 | Orchestration | `run_day` halts on *any* quality issue: 137 issues on 20 clean symbols | **FIXED** — data-quality kinds are partitioned into blocking and advisory; only blocking kinds halt |
| AUDIT-011 | P1 | Calendar | No NSE holiday table; real data contains weekend special sessions flagged as errors | **FIXED** — committed NSE CM calendar with 6 verified special sessions; re-measured 731 → 12 false off-calendar rows |
| AUDIT-012 | P1 | Data integrity | Non-EQ series (BE/SM/ST/BZ — 63% of symbols) are mixed into price history | **FIXED** — EQ filter at the ingestion boundary; re-measured: 14.99% of rows were non-EQ, 741/3694 files have no EQ row |
| AUDIT-013 | P1 | Paper trading | No strategy is `paper_approved`; the rebalance button can never succeed as shipped | **FIXED (partly)** — `momrem` is now in the registry with an explicit refusal reason and the UI publishes why it is blocked; it is deliberately still **not approved** |
| AUDIT-015 | P1 | Integration | `preview_rebalance` bypasses the SIM quote chain the rest of the app uses | **FIXED** |
| AUDIT-016 | P1 | Packaging | The wheel omits the `data` package | **FIXED** |
| AUDIT-017 | P1 | Clean install | `seaborn` is imported but not declared → clean install cannot collect tests | **FIXED** |
| AUDIT-018 | P1 | Deployment | `pip install --no-deps .` → the Docker image crash-loops | **FIXED** |
| AUDIT-019 | P1 | Deployment | `var/` mounted read-only → the kill switch cannot be armed in the container | **FIXED** |
| AUDIT-020 | P1 | Config | `validate_environment()` had no caller outside tests | **FIXED** (preflight added) |
| AUDIT-022 | P1 | Reconciliation | Order/fill reconciliation compares the repository against itself | **FIXED** — reconciliation diffs against the broker’s view and raises `ReconciliationError` |
| AUDIT-023 | P1 | CI | Lint and format both fail at HEAD; CI is red | **FIXED** |
| AUDIT-035 | P1 | Freshness | Shipped data is 7 days old → the signal is "stale" and every paper action is blocked out of the box | **OPEN — operator action**: refresh `data/clean/eod2_data` from the pinned source. A stale-data refusal is the correct behaviour, not a bug to patch |
| AUDIT-032 | P1 | Research gate | DSR trial count under-counts; missing validation is a *warning*, not a failure | **FIXED** — declared trials, out-of-sample evidence and validation are now required; validation missing is a **fail** |
| AUDIT-024 | P2 | Risk | Two independent, conflicting risk-limit systems | **FIXED** — `config/risk_policy.py` is the single source; paper limits derived from the guard, never looser |
| AUDIT-025 | P2 | State machine | `execution/state_machine.py` is a 0-byte file | **FIXED** — `execution/state_machine.py` implemented and wired into `PaperBroker`; illegal transitions raise and leave the ledger intact |
| AUDIT-027 | P2 | Tests | Tests mutate committed data files — and the *number of tests that run* therefore changes between runs (1293 vs 1411 collected on the same tree) | **FIXED** — `QUANT_DATA_DIR` is honoured per test; a full run leaves the working tree clean |
| AUDIT-028 | P2 | Observability | `/healthz` is stdlib-only, so the healthcheck passes while every panel 503s | **FIXED** — `/healthz` probes the real dependencies and returns 503 when one is broken |
| AUDIT-029 | P2 | Config | Staleness threshold: 6 days in `data.quality`, 18 hours in `risk_kill` | **FIXED** — both windows defined together in `config/risk_policy.py` and checked for consistency |
| AUDIT-033 | P2 | Dashboard | The Streamlit dashboard needs `streamlit`, which is not a declared dependency | **FIXED** — `streamlit` is an optional extra; the missing-dependency error is actionable |
| AUDIT-034 | P2 | Dashboard | Reconciliation says "run a paper rebalance" when a rebalance is impossible | **FIXED** — reconciliation detail corrected; `rebalance_blocked_reason` published |
| AUDIT-031 | P3 | **FIXED** — sample regenerated and validated against `OrderResult` | `execution/orders.jsonl` does not match the `OrderResult` schema | **ANALYSED** |
| AUDIT-026 | P3 | **FIXED** — no `sys.argv` index arithmetic | `main.py dashboard --port` parses `sys.argv` inside the default | **ANALYSED** |

---

## AUDIT-001 — Every protective risk decision crashed the execution path

* **Severity:** P0
* **Category:** Risk management / execution safety
* **File:** `execution/service.py`
* **Location:** `ExecutionService.execute_targets`, lines 207–227 (pre-fix)
* **Problem:** The code compared a `risk_kill.RiskState` against `RiskState.HALTED`,
  `RiskState.LOCKED` and `RiskState.WARNING`. Those members do not exist — they belong to
  `observability.health.SystemHealth`.
* **Why:** Two enums with overlapping-looking vocabularies were conflated. `RiskState` is
  `{NOMINAL, ALERT_HUMAN, STOP_NEW_ORDERS, CANCEL_OPEN_ORDERS, FLATTEN_POSITIONS, LOCK_ACCOUNT}`.
* **Failure scenario:** Stale data (or a drawdown breach, or a reconciliation lock) makes the
  guard return `STOP_NEW_ORDERS`. `execute_targets` enters the `if decision.state is not
  RiskState.NOMINAL:` branch and immediately raises `AttributeError: HALTED` on the first
  comparison. The `return ExecutionSummary(..., halted=True)` at the end of that branch is
  unreachable.
* **Expected:** `execute_targets` returns `ExecutionSummary(halted=True, submitted=[])`, writes
  `SystemHealth.HALTED`, and raises a critical alert.
* **Actual (reproduced):** `AttributeError: HALTED`. Verified with a direct reproduction:
  ```python
  # /tmp/repro1.py — stale-data context
  decision = RiskGuard().evaluate(ctx)      # RiskDecision(state=STOP_NEW_ORDERS, ...)
  service.execute_targets(...)              # EXCEPTION: AttributeError HALTED
  ```
  `grep -rn "RiskState\.\(HALTED\|LOCKED\|WARNING\)"` matches only these four lines, so no other
  module compensates.
* **Impact:** The *fail-closed* path of the only execution service in the repository is a
  crash. In `orchestration/pipeline.py` the call sits outside the research `try/except`, so a
  protective halt becomes an unhandled exception that aborts the day instead of recording
  `halted_risk`. An operator reading logs sees a traceback, not "system halted".
* **Confidence:** High — reproduced twice, before and after the fix.
* **Recommended fix:** Centralise the mapping in one total function and make it fail *closed*
  on unknown input. Implemented as `risk_kill/mapping.py`:
  `NOMINAL→HEALTHY`, `ALERT_HUMAN→WARNING`, `STOP_NEW_ORDERS/CANCEL_OPEN_ORDERS/
  FLATTEN_POSITIONS→HALTED`, `LOCK_ACCOUNT→LOCKED`, anything else (including `None`) → `LOCKED`.
  `risk_kill.mapping` imports only stdlib + `RiskState`, preserving the architectural rule that
  `risk_kill` is stdlib-only (asserted by `tests/test_architecture.py`), and it returns the
  *member name* rather than importing `observability.health`.
* **Test required:** `tests/test_forensic_audit_regressions.py::
  test_audit_001_execution_halts_without_raising` (end-to-end: stale context + health/alert
  services ⇒ `summary.halted is True`, no exception, broker untouched) plus the two
  parametrised mapping tests. **All pass.**

---

## AUDIT-002 — A live FRED API key was committed to the repository

* **Severity:** P0
* **Category:** Secrets / security
* **File:** `scripts/ingest_macro.py`
* **Location:** line 7 (pre-fix): `FRED_API_KEY = "0a7fba5965eb42e16d16f0eee41a9bb8"`
* **Problem:** A real, working-looking 32-character credential was hardcoded as a module
  constant with the comment `# The user's FRED API key` and interpolated into every request URL.
* **Why:** Convenience during development; no pre-commit secret scanning caught it.
* **Failure scenario:** Anyone who clones the repository (it is public) obtains the key and can
  spend the owner's FRED quota or have it revoked.
* **Expected:** Credentials come from the environment; absence fails loudly.
* **Actual:** Credential in source, in git history, and in the single commit `80490c3`.
* **Impact:** Credential leak (explicit P0 category). Rate-limit exhaustion breaks the macro
  ingestion for the legitimate owner.
* **Confidence:** High — literal present in the working tree; confirmed present in `git show HEAD`.
* **Recommended fix:** Remove the literal, read `FRED_API_KEY` from the environment, and raise a
  typed `MissingCredential` when absent. **Done.** Then **rotate the key** — deleting a value from
  the working tree does not remove it from git history, and this repository's history has already
  been pushed. Rotation is mandatory, not optional.
* **Test required:** `test_audit_002_no_api_key_literal_in_scripts` (walks every `.py` file) and
  `test_audit_002_macro_script_reads_the_key_from_the_environment`. **Both pass.**
  The regression test deliberately reconstructs the leaked string rather than writing it into the
  repository a second time.

---

## AUDIT-003 — The secret scanner was structurally blind to the leak

* **Severity:** P0
* **Category:** Security control failure
* **File:** `tests/test_architecture.py`
* **Location:** `TestNoSecretsInSource._SECRET_RE` (pre-fix)
* **Problem:** The pattern was
  `r"(?i)\b(api[_-]?key|secret|token|password|passphrase)\b\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"`.
  `\b` is a boundary between a word character and a non-word character. In `FRED_API_KEY`, the
  character before `API` is `_`, which **is** a word character, so `\b` cannot match there and the
  whole pattern fails.
* **Why:** A `\b` anchor was used where "any identifier prefix" was intended. Any
  `<PREFIX>_API_KEY` was invisible.
* **Failure scenario:** The one automated control that should have blocked AUDIT-002 silently
  passes while a live credential sits in the tree. The control provides false assurance.
* **Expected:** `FRED_API_KEY = "<32 hex chars>"` is flagged.
* **Actual:** `re.search(...) → None`, verified directly against the committed file.
* **Impact:** Security control failure (P0). The test suite reported "no hardcoded credentials"
  while one was present.
* **Confidence:** High — reproduced with the exact regex against the exact line.
* **Recommended fix:** Lookarounds that allow a leading underscore (`(?<![A-Za-z0-9])`) plus a
  value-shape heuristic so that environment-variable *names* (`_ENV_TOKEN = "TELEGRAM_BOT_TOKEN"`)
  and doc placeholders (`"your-daily-access-token"`) are not false positives. **Done** —
  `_looks_like_secret()` requires mixed character classes or ≥40 characters and rejects
  ALL-CAPS-with-underscores and placeholder prefixes.
* **Test required:** `test_audit_003_secret_scanner_catches_prefixed_names` — asserts
  `FRED_API_KEY` matches, `_ENV_TOKEN = "TELEGRAM_BOT_TOKEN"` does not, and
  `"your-daily-access-token"` does not. **Passes.**

---

## AUDIT-014 — The research panel fabricates pre-listing prices and calls them "excluded"

* **Severity:** P0
* **Category:** Data integrity / survivorship & look-ahead
* **File:** `research/realdata.py`
* **Location:** `build_market_panels`, lines 175–212 (pre-fix); `_pivot_field` calls
* **Problem:** Two defects in the module whose entire job is to **prevent** survivorship bias:

  1. The docstring says *"incomplete symbols are excluded with an explicit reason (never
     silently dropped) and the panel is built from the complete set"*. The code says
     `# We no longer exclude, but just note it` — the symbol is added to **both** `complete`
     **and** `excluded`. `completeness_report.json` therefore lists it under
     `excluded_symbols` while it is in fact in the panel.
  2. `_pivot_field(...).ffill().bfill()` back-fills a symbol's **first traded price over every
     date before it existed**.

* **Why:** An undocumented behaviour change (the in-code comment is the only record) that was
  never reflected in the docstring, the report schema, or the tests — and the tests that would
  have caught it never ran (see AUDIT-004).
* **Failure scenario (reproduced end-to-end on the repository's own fixture world):**
  ```
  requested: 53   panel symbols: 52
  NEWCO in panel symbols: True
  panels.excluded["NEWCO"] = "incomplete price history in window: 306 calendar
                              day(s) missing (first observation 2024-03-05)"
  NEWCO clean first date: 2024-03-05
  panel first date:       2023-01-02
  NEWCO close rows before its first real observation: 306   NaNs: 0
  NEWCO close head(3): [121.18, 121.18, 121.18]      # first real close = 121.18
  => back-filled constant before listing: True
  ```
  NEWCO has a **flat, fabricated price for 306 sessions** before it listed.
* **Expected:** Either the symbol is excluded (per the docstring), or its pre-listing prices are
  `NaN` so downstream code can see the gap.
* **Actual:** Fabricated constant prices plus a completeness report that claims the symbol was
  excluded.
* **Impact:** Invalidated backtests (explicit P0 category). Every momentum, volatility and
  correlation computed over the window is contaminated:
  * a back-filled flat series has **zero** realised volatility before listing, so
    `InverseVolatilityConstructor` assigns it a near-infinite weight (it divides by
    `volatility.replace(0, nan)` — a flat series is caught, but a *mostly* flat one is not);
  * a 63/126/252-day momentum computed just after listing reads ≈0 rather than "no history",
    so a fresh listing competes in the cross-sectional ranking as a legitimate candidate;
  * the market proxy (`market_proxy` = equal-weight of the whole panel) is dragged toward flat
    for the whole pre-listing period, which shifts the regime filter's 100-day SMA gate.
* **Confidence:** High — reproduced with an executable probe against the fixture world.
* **Recommended fix:** Two independent, ordered changes:
  1. **Honest reporting (safe, immediate):** rename the concept. `excluded` ⇒ `notes`, and
     expose `incomplete_symbols` + `price_fill` on `ResearchPanels`. Patch in
     `FIX_PLAN.md` §"AUDIT-014 reporting".
  2. **Correct behaviour (needs a re-baseline):** default `exclude_incomplete=True` and
     `fill_missing_prices=False`, then re-run every published experiment. This **will** change
     every published number and must not be done silently.
  I implemented (1) and added opt-in flags for (2) **with the historical defaults preserved**, so
  no published number moves without an explicit decision.
* **Test required:** `test_audit_014_panel_backfills_prices_before_listing` pins all three
  behaviours (default = back-filled and kept; `exclude_incomplete=True` = dropped;
  `fill_missing_prices=False` = NaN). **Passes.** The three tests in
  `tests/test_real_data_pipeline.py` that encode the documented contract are marked
  `xfail(strict=True)` — they will turn into hard failures the moment the default flips, which is
  the intended forcing function.

---

## AUDIT-021 — The kill switch does not gate execution, and the guard has no persistence

* **Severity:** P0
* **Category:** Kill switch / risk
* **Files:** `datahub/state.py`, `risk_kill/guard.py`, `risk_kill/state.json`,
  `execution/service.py`, `orchestration/pipeline.py`
* **Location:** whole-system
* **Problem:** There are **two unrelated kill-switch mechanisms** and neither is complete.

  1. `risk_kill/state.json` ships a hand-written document
     (`{"status": "ARMED", "tripped": false, ...}`). `grep -rn "risk_kill/state"` finds it
     referenced **only** by `dashboard/main_dashboard.py` (the Streamlit dashboard, which is not
     the served one) and `scripts/generate_sample_data.py` (sample-data generator).
     `RiskGuard` has **no persistence of any kind** — it is a pure function evaluated per call.
     So the deterministic guard's protective state **does not survive a restart**, and the file
     that implies it does is dead.
  2. The live operator switch is `datahub.state` (`var/system_state.json`). It *is* persisted
     (atomic write, 200-entry history). But `grep -rn "is_killed"` shows it is consulted by
     exactly three places: `dashboard/operations.py`, `dashboard/server.py`
     (`POST /api/kill-switch`), and `paper_trading/service.py` (2 call sites).
     `execution/service.py` and `orchestration/pipeline.py` **never call it**.

* **Why:** The two layers were built at different times and never joined.
* **Failure scenario:** An operator arms the kill switch on the Operations page. Paper
  rebalances stop. But a `DailyPipeline.run_day` scheduled by `scripts/run_daily.py` proceeds
  through validation → signals → risk guard → approval gate → `execution_service.execute_targets`
  and submits orders, because nothing in that chain reads `datahub.state.is_killed()`.
* **Expected:** Arming the switch stops **every** order-creating path, including orchestration and
  execution; the deterministic guard's state survives a process restart.
* **Actual:** Arming stops the paper service and the demo bot only.
* **Impact:** Bypassed kill switch (explicit P0 category).
* **Confidence:** High — verified by grep over the whole repository and by reading both files.
* **Recommended fix:** Two parts, both behavioural, therefore **not applied automatically**:
  1. Add a single `KillSwitch` authority in `datahub.state` and call `is_killed()` at the top of
     `ExecutionService.execute_targets` and `DailyPipeline.run_day`, returning the same
     fail-closed summary used for a `LOCK_ACCOUNT` decision. Precise patch in `FIX_PLAN.md`.
  2. Give `RiskGuard` an optional persister so `LOCK_ACCOUNT` survives a restart, or delete
     `risk_kill/state.json` as misleading dead code. Deleting is the honest minimum.
* **Test required:** `test_kill_switch_blocks_orchestrated_execution` and
  `test_risk_state_survives_restart` — both specified in `TEST_GAP_ANALYSIS.md`, neither written
  (they assert behaviour that does not yet exist; writing them would mean changing a safety
  control, which is out of scope for automatic fixes).

---

## AUDIT-030 — Three risk checks are structurally dead in the orchestrated path

* **Severity:** P0
* **Category:** Risk management
* **File:** `orchestration/pipeline.py`
* **Location:** `DailyPipeline._risk_context`, lines 230–260
* **Problem:**
  ```python
  return RiskContext(
      ...
      equity_now=equity,
      equity_day_start=equity,     # identical by construction
      equity_peak=self._equity_peak,   # seeded from the first equity this process sees
      ...
      broker_connected=True,       # hardcoded
      order_timestamps=(),         # always empty
  )
  ```
* **Why:** The context builder has no access to a previous day's equity snapshot or to a real
  broker heartbeat, so it filled the fields with placeholders rather than raising.
* **Failure scenario:**
  * `check_daily_loss` computes `equity_now - equity_day_start`, which is **always 0.0** ⇒ the
    3% daily-loss limit can never trip.
  * `check_broker_connectivity` sees `True` ⇒ it can never trip.
  * `check_order_rate` sees an empty tuple ⇒ it can never trip.
  * `check_drawdown` compares against `self._equity_peak`, which is seeded from the **first equity
    this process observes**, so after a restart the entire prior drawdown is forgotten — a
    portfolio that has already fallen 9% starts from a fresh peak.
* **Expected:** All six risk checks are live in the orchestrated path.
* **Actual:** Three are dead; the fourth is only meaningful within a single process lifetime.
* **Impact:** Risk checks silently disabled (P0 — "bypassed risk"). The system reports
  `NOMINAL` while being unable to detect a daily-loss breach at all.
* **Confidence:** High — read directly; the arithmetic is trivially verifiable.
* **Recommended fix:** Source `equity_day_start` and `equity_peak` from the persisted equity
  history (`paper_trading` ledger, or a new `equity_snapshots` table), derive `broker_connected`
  from an actual heartbeat with a freshness bound, and pass the real order timestamps. If any
  input is unavailable the context builder must **fail closed** (`broker_connected=None` already
  maps to `LOCK_ACCOUNT` in `RiskGuard.check_broker_connectivity`) — it must not substitute a
  benign value. Patch in `FIX_PLAN.md`.
* **Test required:** `test_pipeline_risk_context_uses_persisted_equity`, specified in
  `TEST_GAP_ANALYSIS.md`.

---

## AUDIT-039 — The dashboard has no authentication on any route, and it can disarm the kill switch

* **Severity:** P0
* **Category:** Security / kill switch
* **File:** `dashboard/server.py`
* **Location:** `DashboardHandler.do_GET` / `do_POST` (835 lines, no auth check anywhere);
  `run_server` line 820: `ThreadingHTTPServer(("0.0.0.0", actual_port), DashboardHandler)  # nosec B104`
* **Problem:** There is no authentication, authorisation, CSRF token, origin check, or rate
  limit on any route. `grep -n "auth\|Authorization\|token\|Access-Control" dashboard/server.py`
  returns exactly one hit — a response header for SSE buffering. The server binds `0.0.0.0`,
  and the `# nosec B104` comment shows the binding warning was deliberately suppressed without
  adding any control.
* **Why:** It is treated as a local development tool and shipped as the production entry point
  (`Dockerfile` `ENTRYPOINT ["python", "-m", "dashboard.server"]`).
* **Failure scenario:** Anyone who can reach port 8080 can
  `POST /api/kill-switch {"armed": false}` — **disarming the operator kill switch** — as well as
  `POST /api/paper/reset`, `POST /api/paper/rebalance`, `POST /api/paper/automation`,
  `POST /api/data/rebuild-prices` and `POST /api/universe/expand`. `GET /api/paper/audit` and
  `GET /api/paper/export` expose the full ledger.
* **Expected:** Mutating routes are authenticated and authorised; the kill switch in particular
  requires a privileged operator.
* **Actual:** Wide open. Verified: the server responds to unauthenticated POSTs, and the source
  contains no check.
* **Impact:** Kill switch can be disarmed by an unauthenticated third party (P0 — "bypassed
  kill-switch"), plus unauthenticated state destruction (`/api/paper/reset`).
* **Confidence:** High — read the entire file and exercised the routes over HTTP without
  credentials.
* **Recommended fix:** Do not ship this entry point without a control in front of it. Minimum
  viable, in order of preference: (1) bind to `127.0.0.1` and require an SSH tunnel / reverse
  proxy with auth — the `# nosec B104` is only defensible on loopback; (2) add a
  `QUANT_DASHBOARD_TOKEN` shared-secret header check on every non-GET route; (3) add an origin
  check plus CSRF token for state changes. **Not applied** — choosing an auth model is a
  product decision, and adding one silently could lock operators out. Patch in `FIX_PLAN.md` §B12.
* **Test required:** `test_dashboard_mutating_routes_require_auth` — unauthenticated POST to
  every mutating route returns 401/403.

---

## AUDIT-004 — The real-data pipeline could not be invoked at all

* **Severity:** P1
* **Category:** Real-data pipeline / test integrity
* **Files:** `scripts/ingest_real_data.py`, `scripts/run_real_data_experiment.py`,
  `data/universe.py`, `tests/test_real_data_pipeline.py`
* **Location:** `main()` argparse; `ingest_membership`; `build_completeness_report`;
  `UniverseDataset.from_dir`
* **Problem:** Four separate defects, each of which individually broke the pipeline:

  1. The test harness invokes the CLI with `--universe-dir`; the CLI only defined
     `--universe-root`. `argparse` exits with status 2, so the module-scoped fixture
     `ingested_world` raised `SystemExit: 2` and **9 of 22 tests in the only real-data test
     module never executed** (7 errors + 2 failures recorded as fixture errors).
  2. `ingest_membership` iterated Nifty 50 / 100 / 500 and called
     `extract_index_rows(..., index_name=...)`, which **raises** when an index has no rows.
     One absent index aborted the whole ingestion.
  3. `ingest_membership` returns `{slug: audit}` but `build_completeness_report` reads
     `report["universe"]["rows"]` at the top level ⇒ `KeyError: 'rows'` — the completeness
     report, the artefact the pipeline exists to produce, could never be rendered.
  4. `UniverseDataset.from_dir` skipped sub-directories, so pointing it at the universe root
     (which holds `<slug>-pit/`) raised `ValueError: no universe membership files found`.
     Separately, `run_real_data_experiment.py`'s `--universe-dir` means the *per-index*
     directory while `ingest_real_data.py`'s means the *root* — same flag, opposite meaning.

* **Why:** A CLI rename that was never propagated, plus a return-shape change that was never
  propagated, in a module whose tests could not run to catch either.
* **Failure scenario:** `python scripts/ingest_real_data.py --local --universe-dir …` exits 2.
  Nine tests silently error. The completeness report crashes.
* **Expected:** The documented end-to-end real-data flow runs.
* **Actual:** It could not start.
* **Impact:** The entire v0.7 real-data path — ingestion, PIT universe, completeness report,
  ingestion→research integration, reproducibility — was untested *and* broken. The most
  important tests in the repository were dead.
* **Confidence:** High — each defect reproduced; the module went from
  `2 failed, 7 errors, 13 passed` to `21 passed, 3 xfailed` after the fixes.
* **Recommended fix:** Accept `--universe-dir` as an alias of `--universe-root`; skip absent
  indices but fail closed when **all** are absent; merge the per-index audit into report shape
  (`merge_membership_audit`); make `from_dir` descend one level; resolve either universe spelling
  in `run_real_data_experiment`. **All done.**
* **Test required:** `test_audit_004_ingest_cli_accepts_universe_dir`,
  `test_audit_004_run_experiment_resolves_universe_root`,
  `test_audit_004_membership_audit_merges_to_report_shape`,
  `test_audit_004_universe_dataset_from_dir_descends`. **All pass**, plus the 21 tests in
  `tests/test_real_data_pipeline.py` that now actually execute.

---

## AUDIT-005 — A malformed source header crashes the eod2 adapter

* **Severity:** P1
* **Category:** Ingestion
* **File:** `ingestion/eod2_adapter.py`
* **Location:** `parse_eod2_daily_file` / `_read_raw_csv`
* **Problem:** `EOD2_DAILY_HEADER` and the module docstring advertise an "order-sensitive strict
  header check"; **no header validation existed**. With the `Date` column missing, pandas promotes
  the first data column to the index, `raw.get("Date")` returns `None`,
  `pd.to_datetime(None, errors="coerce")` yields a non-datetime, and
  `frame["date"].dt.strftime(...)` raises
  `AttributeError: Can only use .dt accessor with datetimelike values`.
* **Why:** The check was documented and tested (`test_adapter_missing_field_raises` expects
  `DataQualityError`) but never implemented.
* **Failure scenario:** A truncated or renamed upstream file turns an operator-facing data
  problem into an opaque traceback.
* **Expected:** `DataQualityError("unexpected header …")`.
* **Actual:** `AttributeError`.
* **Impact:** Ingestion crashes instead of reporting; the failure is not attributable to a source
  file.
* **Confidence:** High — the existing test failed with exactly this traceback.
* **Recommended fix:** Validate the load-bearing OHLCV prefix (`Date..Volume`) in order, while
  tolerating the two upstream trailing-column dialects (`TOTAL_TRADES/QTY_PER_TRADE/DLV_QTY` and
  the lowercase variant). **Done.**
* **Test required:** `test_audit_005_truncated_header_is_a_data_quality_error` and
  `test_audit_005_both_upstream_header_dialects_parse`. **Both pass.**

---

## AUDIT-006 — The look-ahead (future-date) guard never ran

* **Severity:** P1
* **Category:** Look-ahead bias
* **Files:** `data/quality.py`, `orchestration/pipeline.py`
* **Location:** `validate_market_bars` (missing `as_of` forwarding); `run_day` step 1
* **Problem:** `check_ohlcv_long_frame` implements a `future_date` issue kind, but
  `validate_market_bars` — the function `orchestration/pipeline.py` calls — never passed `as_of`.
  The pipeline computed `as_of` *after* validation, from the frame's own maximum date, which
  makes the guard a tautology: a frame containing tomorrow's bars validates itself.
* **Why:** The parameter existed on the lower-level function and was never threaded through.
* **Failure scenario:** A vendor file with future-dated rows (or a clock/timezone bug producing
  tomorrow's date) is accepted and traded on.
* **Expected:** Bars dated after the as-of boundary are reported and excluded.
* **Actual:** Accepted silently.
* **Impact:** Silent look-ahead on the orchestrated path.
* **Confidence:** High — verified by reading both functions and by an execution test that fails
  before the fix.
* **Recommended fix:** Thread `as_of` through `validate_market_bars` and default `run_day`'s
  `as_of` to **today in IST** (never to the frame's max). **Done.**
* **Test required:** `test_audit_006_validate_market_bars_rejects_future_bars` and
  `test_audit_006_pipeline_defaults_as_of_to_today_not_frame_max`. **Both pass.**

---

## AUDIT-007 — The survivorship-bias guard is a mandatory but unused parameter

* **Severity:** P1
* **Category:** Survivorship bias
* **File:** `backtest/engine.py`
* **Location:** `VectorBTResearchEngine.run`, signature + first statement
* **Problem:** `run()` raises a stern `ResearchInputError` when `universe_history is None`
  ("*Backtests must explicitly provide historical index membership to prevent survivorship bias.
  Do not use today's universe for history.*"). The parameter is then **never referenced again**
  anywhere in the method.
* **Why:** The guard was designed at the interface and never implemented in the body.
* **Failure scenario:** Callers dutifully pass `universe_history=[]` (as
  `generate_placebo_results` and `backtest/validation._evaluate_windows` both do) or
  `universe_history=[1]`, and the backtest runs with zero survivorship protection while the
  signature implies there is one.
* **Expected:** Passing membership data restricts the tradable cross-section per date.
* **Actual:** Nothing changes. Verified: `universe_history=[]` and
  `universe_history=["NOT_A_SYMBOL"]` produce **bit-identical** return series and Sharpe.
* **Impact:** The primary anti-survivorship-bias control is decorative (P1). Combined with
  AUDIT-014 (fabricated pre-listing prices), the research layer has effectively **no**
  survivorship protection despite two separate mechanisms claiming otherwise.
* **Confidence:** High — verified with an execution test comparing both invocations.
* **Recommended fix:** Implement it: accept `Sequence[Mapping[date, Sequence[str]]] | pd.DataFrame`
  and mask `prices`/`targets` to members on each date. **Not applied** — it would change every
  published number and every caller currently passes a placeholder. Precise patch and migration
  order in `FIX_PLAN.md`; behaviour pinned by
  `test_audit_007_universe_history_is_required_but_unused`.

---

## AUDIT-008 — The backtest mixes two execution models in one result

* **Severity:** P1
* **Category:** Backtest correctness
* **File:** `backtest/engine.py`
* **Location:** `run()` — `vectorbt_output` vs `trades`
* **Problem:** When VectorBT succeeds, `returns`/`equity_curve` come from VectorBT, but
  `trades`, `turnover`, `total_cost` and `trade_count` are **always** computed by
  `_simulate_pandas`. `compute_performance_metrics` then receives VectorBT returns together with
  pandas-derived turnover and cost.
* **Why:** The pandas path was retained as a fallback and its trade table was reused for reporting.
* **Failure scenario (measured, 30 assets × 2 600 business days, monthly rebalance, top-5
  momentum):**

  | Costs | Backend | Sharpe | Total return | `total_cost` reported |
  | --- | --- | --- | --- | --- |
  | zero | vectorbt | 1.2678 | 3.0818 | 0.00000 |
  | zero | pandas | 1.2544 | 3.0435 | 0.00000 |
  | india/base | vectorbt | 0.9184 | 1.7248 | 0.33385 |
  | india/base | pandas | 0.9657 | 1.8952 | 0.33385 |
  | india/pessimistic | vectorbt | 0.7589 | 1.2679 | 0.48543 |
  | india/pessimistic | pandas | 0.8329 | 1.4869 | 0.48543 |

  The two backends disagree **even with zero costs** (≈1.3% divergent total return, from
  share/cash accounting vs weight-based returns), and by 5% on Sharpe / 10% on total return once
  costs are applied — while reporting an identical `total_cost`.
* **Expected:** One execution model per result; reported costs reconcile with the reported equity
  curve.
* **Actual:** Returns from one simulation, costs and turnover from another.
* **Impact:** Research results depend on whether `vectorbt` happens to import in the environment
  (`use_vectorbt=True` by default, with a **silent** fallback on any exception — only
  `metadata["backend"]` records it). `metrics.cost_drag` is not reconcilable with
  `metrics.total_return`. A P&L attribution built on this will not add up.
* **Confidence:** High — measured directly.
* **Recommended fix:** Compute the trade table from whichever backend produced the returns, or
  (simplest, and the option I recommend) make the pandas path authoritative and record
  `backend: vectorbt` results as advisory-only. At minimum, log a warning and stamp
  `metadata["cost_accounting_backend"]`. Patch in `FIX_PLAN.md`. **Not applied** — it changes
  published numbers.
* **Test required:** `test_backends_agree_on_zero_cost` (a cross-backend reconciliation test),
  specified in `TEST_GAP_ANALYSIS.md`.

---

## AUDIT-009 — The backtest silently forward- and back-fills the price panel

* **Severity:** P1
* **Category:** Backtest correctness / data integrity
* **File:** `backtest/engine.py`
* **Location:** `_validate_inputs`:
  `numeric_prices = prices.apply(pd.to_numeric, errors="coerce").ffill().bfill()`
* **Problem:** Missing prices are forward-filled and then back-filled, and missing returns are
  `fillna(0.0)`. A halted or delisted instrument is carried forward forever at its last price,
  producing a long run of fake zero returns.
* **Why:** Convenience, so that the panel is rectangular.
* **Failure scenario:** A delisted name is held at a frozen price for the rest of the backtest;
  its realised volatility collapses toward zero, so inverse-volatility weighting assigns it a
  large weight.
* **Expected:** Gaps are reported and either excluded or carried as `NaN` with explicit handling.
* **Actual:** Silently imputed.
* **Impact:** Directly contradicts `data/quality.py`'s own stated rule — *"The system does not
  impute prices"* — and contradicts the `MarketData` contract. Two modules in the same repository
  assert opposite policies.
* **Confidence:** High — read directly.
* **Recommended fix:** Do not fill. Reject or mask non-finite prices, and let the constructor
  decide (both `equal_weight` and `inverse_volatility` already treat a non-positive/NaN signal as
  "no position", which is the fail-closed behaviour). Patch in `FIX_PLAN.md`. **Not applied.**
* **Test required:** `test_backtest_rejects_or_masks_price_gaps`.

---

## AUDIT-010 — The daily pipeline halts on any data-quality issue, so it can never run on real data

* **Severity:** P1
* **Category:** Orchestration
* **File:** `orchestration/pipeline.py`
* **Location:** `run_day` step 1: `if accepted.empty or report.issues: → halted_data_quality`
* **Problem:** The pipeline halts on **any** issue of **any** kind. `detect_missing_candles`
  emits one issue per (symbol, date) gap against the union calendar, so the issue count scales
  with the universe.
* **Failure scenario (measured on this repository's own clean data, 20 symbols, 2024-01-01 →
  2026-08-25, 11 504 rows):**
  ```
  total_rows 11504   accepted 11504
  issues by kind: {'staleness': 1, 'missing_candle': 75, 'off_calendar': 61}
  is_clean: False
  ```
  **Zero rows were actually rejected** — not one row failed validation — yet `report.issues` is
  non-empty, so `run_day` halts. A perfectly ordinary 20-symbol dataset stops the system.
  Extrapolated to the 133-symbol clean bundle this is thousands of issues.
* **Expected:** Rejected/unusable rows halt the day; ordinary gaps in an illiquid name are
  reported and tolerated.
* **Actual:** Any gap anywhere halts everything.
* **Impact:** The orchestration pipeline is unusable outside the 2-symbol test fixture
  (`tests/test_orchestration.py` uses RELIANCE + TCS). This is a textbook "works on toy data,
  collapses on real data" failure.
* **Confidence:** High — measured on committed data.
* **Recommended fix:** Classify issue kinds into blocking (`invalid_*`, `duplicate_row`,
  `future_date`, `staleness`) and advisory (`missing_candle`, `off_calendar`), keep the advisory
  set in the report with a configurable cap, and record `halted_data_quality` only for blocking
  kinds. This is a **policy change to a safety control**, so it is **not applied** — precise patch
  in `FIX_PLAN.md` §"AUDIT-010".
* **Test required:** `test_pipeline_runs_on_real_multi_symbol_data`.

---

## AUDIT-011 — No NSE holiday calendar anywhere

* **Severity:** P1
* **Category:** Indian-market domain
* **File:** `data/quality.py`
* **Location:** `TradingCalendar`, `nse_weekday_calendar()`
* **Problem:** The only calendar factory is "every weekday". `detect_off_calendar_candles` can
  therefore only ever detect weekend bars, and `detect_missing_candles` expects trading on every
  weekday including NSE holidays (Diwali/Laxmi Pujan, Holi, Id, Republic Day, Independence Day,
  Gandhi Jayanti, Christmas, …).
* **Failure scenario (measured on committed data):** the clean bundle contains **50 Saturday and
  11 Sunday candles** across 20 symbols. Some are legitimate NSE special sessions — 2025-02-01
  (Saturday, Union Budget) and 2026-02-01 (Sunday, Union Budget) are real trading days — and are
  flagged as `off_calendar` errors. Meanwhile actual holidays are never detected because the
  calendar does not know they exist.
* **Expected:** A maintained NSE trading calendar with holiday and special-session data.
* **Actual:** Weekday-only. Both false positives (special sessions) and false negatives
  (holidays).
* **Impact:** Every calendar-derived number — annualisation, staleness, missing-candle detection,
  session boundaries — is wrong around Indian holidays. `TradingCalendar.is_trading_day()` returns
  `True` on Diwali.
* **Confidence:** High.
* **Recommended fix:** Load a maintained NSE holiday list (NSE publishes `FO_master` / the
  exchange holiday circular) into `TradingCalendar` and add an explicit special-session set.
  Documented in `FIX_PLAN.md`; not applied (requires an authoritative external data file).

---

## AUDIT-012 — Non-equity series are mixed into price history

* **Severity:** P1
* **Category:** Data integrity
* **File:** `ingestion/eod2_adapter.py`
* **Location:** `parse_eod2_daily_file` — the `Series` column is parsed but never filtered
* **Problem:** NSE daily files carry multiple series. Measured across the first 600 committed
  files: `EQ 1 145 575`, `BE 86 775`, `SM 51 847`, `ST 10 175`, `BZ 2 523` — roughly **8% of all
  rows are non-EQ**, spread across **377 of 600 symbols (63%)**. The adapter loads them all into
  one continuous per-symbol series.
* **Why:** The `Series` column is carried for provenance but not used as a filter.
* **Failure scenario:** BE (book-entry / trade-for-trade), SM, ST and BZ segments have different
  liquidity, price bands and settlement. Splicing them into one price series creates artificial
  jumps at series transitions and pollutes realised volatility.
* **Expected:** `Series == "EQ"` only (or an explicit, documented choice), with other series
  reported.
* **Actual:** Everything is loaded.
* **Impact:** Contaminated price history for two thirds of the universe.
* **Confidence:** High — measured on committed data. (No (symbol, date) duplicates arise, so the
  quality layer's duplicate check does not catch it.)
* **Recommended fix:** Filter to `EQ` in `parse_eod2_daily_file`, record the dropped series in the
  provenance manifest. **Not applied** — it changes the clean layer and therefore every
  downstream number. Patch in `FIX_PLAN.md`.
* **Test required:** `test_adapter_filters_non_eq_series`.

---

## AUDIT-013 — No strategy is paper-approved, so the rebalance button can never work

* **Severity:** P1
* **Category:** Paper trading / functional completeness
* **Files:** `config/paper_strategies.json`, `paper_trading/service.py`
* **Location:** `DEFAULT_REGISTRY`, `is_paper_approved`, `preview_rebalance`
* **Problem:** `config/paper_strategies.json` lists 30 strategies, **all**
  `"paper_approved": false`. The one strategy with a registered target builder, `momrem`, is not
  in the file at all, so `DEFAULT_REGISTRY`'s `paper_approved: False` applies.
* **Failure scenario (reproduced against the running server):**
  ```
  POST /api/paper/start    -> 200
  POST /api/paper/preview  {"strategy_id":"momrem"}
       -> {"error": "strategy is not paper-approved by the research gate"}
  POST /api/paper/rebalance {"strategy_id":"momrem","confirmation":"PAPER REBALANCE"}
       -> {"error": "strategy is not paper-approved by the research gate"}
  ```
* **Expected:** The documented daily workflow — configure, start, preview, rebalance — completes.
* **Actual:** The only order-creating workflow in the product is unreachable as shipped.
* **Impact:** The headline paper-trading feature cannot be exercised end-to-end by a user who
  clones the repository.
* **Confidence:** High — reproduced over HTTP against a live server.
* **Recommended fix:** **Do not** flip the flag to make the button light up — that is exactly the
  "weaken a safety control to make a test pass" failure mode. The correct actions are:
  1. Add `momrem` to `config/paper_strategies.json` with an explicit `reason` so the registry is
     consistent with `DEFAULT_REGISTRY`.
  2. Surface `rebalance_blocked_reason` in `PaperTradingService.status()` so the UI can explain
     *why* the button is disabled instead of failing on click.
  3. Document the approval procedure (gate PASS + the registry entry) in the runbook.
  Patch in `FIX_PLAN.md`. **Not applied** (it is a gating decision for the owner).
* **Test required:** `test_rebalance_blocked_when_not_paper_approved` (pins the fail-closed
  behaviour) — specified in `TEST_GAP_ANALYSIS.md`.

---

## AUDIT-015 — The paper rebalance bypassed the quote-degradation chain

* **Severity:** P1
* **Category:** Integration / misleading UI
* **File:** `paper_trading/service.py`
* **Location:** `preview_rebalance` (pre-fix): `self.market_data.fetch_quotes(...)`
* **Problem:** `refresh_quotes` uses `self._quote_chain()` (UPSTOX → SIM → EOD, explicitly built
  so a missing token degrades to **clearly-labelled** simulated prices). `preview_rebalance` — the
  only method that creates virtual orders — called `UpstoxMarketData.fetch_quotes` **directly**,
  bypassing the chain entirely.
* **Why:** Two code paths reached the same client by different routes.
* **Failure scenario:** Without `UPSTOX_ACCESS_TOKEN` the dashboard tape renders happily (SIM
  quotes, labelled), the Operations page shows `last_quote_detail: {quoted: 5, requested: 5,
  source: "UPSTOX"}` — and pressing Rebalance fails with
  `configure UPSTOX_ACCESS_TOKEN; API key/secret alone cannot fetch quotes`.
  Reproduced offline:
  `ready: False reason: configure UPSTOX_ACCESS_TOKEN; API key/secret alone cannot fetch quotes`
* **Expected:** One pricing path for display *and* execution, with the source recorded on every
  fill.
* **Actual:** Display degrades gracefully; execution hard-fails.
* **Impact:** The UI advertises a working tape while the action the tape exists for is blocked;
  and before the fix every fill was stamped `upstox_quote_read_only` regardless of where the
  price came from — an audit trail that lies.
* **Confidence:** High — reproduced.
* **Recommended fix:** Route `preview_rebalance` through `self._quote_chain()`, keep the explicit
  instrument-map validation, and derive the fill's `source` from the actual quote source
  (`upstox|sim|eod`) so a simulated fill is never labelled as an Upstox one. **Done** —
  `preview` now also returns `quote_sources`, and `_fill_source()` stamps each order.
* **Test required:** `test_preview_uses_the_quote_chain` and
  `test_fill_source_reflects_the_quote_source`, specified in `TEST_GAP_ANALYSIS.md` (the
  repository has no offline unit test for the rebalance path — that gap is itself the reason this
  bug survived).

---

## AUDIT-016 — The wheel does not contain the `data` package

* **Severity:** P1
* **Category:** Packaging / deployment
* **File:** `pyproject.toml`
* **Location:** `[tool.setuptools] packages = [...]`
* **Problem:** `"data"` is missing from the package list (as are `migrations/` and the dashboard's
  static assets).
* **Failure scenario (reproduced):** building the wheel and inspecting it:
  ```
  data/     in wheel: False
  datahub/  in wheel: True
  ```
  and importing `data.quality` from outside the checkout fails with
  `ModuleNotFoundError: No module named 'data'`.
* **Expected:** `pip install quant-india` gives a working install.
* **Actual:** Any non-editable install is broken for every module that touches the data layer —
  including `orchestration.pipeline`. It only appears to work in the Docker image because
  `python -m dashboard.server` puts the CWD on `sys.path`.
* **Impact:** `pip install .` produces an unusable package; the safety margin is an accident of
  `python -m`.
* **Confidence:** High — built the wheel and listed its contents.
* **Recommended fix:** Add `"data"` to `packages`, and ship `dashboard/app/*` and `migrations/*`
  as package data. **Done** for `data`.
* **Test required:** `test_audit_016_wheel_contains_the_data_package`. **Passes.**

---

## AUDIT-017 — `seaborn` is imported but not declared

* **Severity:** P1
* **Category:** Clean install
* **File:** `pyproject.toml`, `dashboard/strategy_performance.py`
* **Location:** `(dashboard/strategy_performance.py:43) import seaborn as sns`
* **Problem:** `seaborn` is imported at module scope but is absent from `dependencies` and from
  `dev`.
* **Failure scenario (reproduced on a clean venv built from `pyproject.toml`):**
  ```
  ERROR collecting tests/test_strategy_performance.py
  dashboard/strategy_performance.py:43: ModuleNotFoundError: No module named 'seaborn'
  ```
  A clean `pytest tests/` run cannot even **collect**.
* **Expected:** `pip install -e .[dev] && pytest tests/` works on a fresh machine.
* **Actual:** Collection error.
* **Impact:** Clean-room install test fails (an explicit item in the audit brief).
* **Confidence:** High — reproduced.
* **Recommended fix:** Declare `seaborn>=0.13.0`. **Done.**
* **Test required:** `test_audit_017_seaborn_is_declared`. **Passes.**

---

## AUDIT-018 — The Docker image cannot start

* **Severity:** P1
* **Category:** Deployment
* **File:** `Dockerfile`
* **Location:** `RUN python -m pip install --no-cache-dir --no-deps .`
* **Problem:** `--no-deps` installs the project with **none** of pandas/numpy/pyarrow/pydantic.
* **Failure scenario (reproduced by blocking third-party imports and starting the server):**
  ```
  get_paper_service FAILED: ImportError BLOCKED third-party import: numpy
  /api/overview   FAILED: ImportError BLOCKED third-party import: numpy
  /api/operations FAILED: ImportError BLOCKED third-party import: numpy
  ```
  `run_server()` calls `get_paper_service()` **before** binding the port, so the process exits
  immediately — the container crash-loops rather than serving a degraded page.
* **Expected:** `docker compose up` serves the dashboard.
* **Actual:** Crash loop.
* **Impact:** The documented deployment path does not work on a fresh machine.
* **Confidence:** High — reproduced by import-blocking (docker is not available in this sandbox).
* **Recommended fix:** `pip install --no-cache-dir .` (with dependencies). **Done.**
* **Test required:** `test_audit_018_dockerfile_installs_dependencies`. **Passes.**
  A real image build could not be executed here — flagged in `AUDIT_VERIFICATION.md`.

---

## AUDIT-019 — The kill switch cannot be armed inside the container

* **Severity:** P1
* **Category:** Deployment / kill switch
* **File:** `docker-compose.yml`
* **Location:** `- ./var:/app/var:ro` combined with `read_only: true`
* **Problem:** `datahub.state.DEFAULT_STATE_FILE` is `var/system_state.json` — the operator kill
  switch — and `QUANT_PAPER_DB` defaults to `var/paper_trading.sqlite`. Both are inside a
  read-only bind mount on a read-only root filesystem.
* **Failure scenario:** `POST /api/kill-switch` → `PermissionError` → caught by the handler's
  generic `except` → `503 {"error": "kill switch unavailable"}`. The paper service cannot open
  its database at all.
* **Expected:** The kill switch is armable in every deployment.
* **Actual:** Un-armable in the only deployment the repository ships.
* **Impact:** Bypassed kill switch in the production deployment. Note the failure is *loud* (503),
  not silent — hence P1 rather than P0 — but the operator's only mitigation is to leave the
  container.
* **Confidence:** Medium-High — the mount flags and write paths are unambiguous; the container
  could not be started here to observe the 503 directly.
* **Recommended fix:** Mount `./var:/app/var` read-write (keep `./data` read-only) and add a tmpfs
  for the runtime user's HOME. **Done.**
* **Test required:** `test_audit_019_compose_var_is_writable`. **Passes.**

---

## AUDIT-020 — The environment validator was never called

* **Severity:** P1
* **Category:** Configuration / fail-closed
* **File:** `config/env_validator.py`
* **Location:** `validate_environment()`
* **Problem:** `grep -rn "validate_environment"` finds only its own definition and
  `tests/test_observability.py`. No application entry point, script, server or pipeline calls it.
* **Why:** It was written as a gate but never wired in.
* **Failure scenario:** A deployment with `SYSTEM_MODE=PRODUCTION`, no `DATABASE_URL`, no
  Telegram alerting, and `UPSTOX_API_SECRET` in the environment starts anyway. Every one of those
  is supposed to be fatal.
* **Expected:** Deployment fails closed on a policy violation.
* **Actual:** Nothing checks.
* **Impact:** The repository's own deployment-safety policy is inert.
* **Confidence:** High.
* **Recommended fix:** Add `scripts/preflight.py` (env checks, optional `--db`, `--json`, non-zero
  exit) and run it before any long-lived process. **Done.** I deliberately did **not** wire it into
  `dashboard/server.py` startup, because `UPSTOX_API_KEY` is documented as optional in
  `.env.example` while `validate_environment` treats it as fatal — wiring it in would break
  existing deployments. That contradiction is itself finding AUDIT-036 below.
* **Test required:** `test_audit_020_preflight_runs_the_environment_policy` and
  `test_audit_020_preflight_rejects_live_broker_credentials`. **Both pass.**

---

## AUDIT-022 — Order reconciliation compares the repository against itself

* **Severity:** P1
* **Category:** Reconciliation
* **Files:** `orchestration/pipeline.py`, `reconciliation/engine.py`
* **Location:** `_expected_state()` vs `_all_broker_orders()` (pipeline, lines 301 / 626)
* **Problem:** Both read `self.order_repository`. `_expected_state` builds
  `expected_open_orders` / `expected_filled` from stored `OrderResult`s; `actual_orders` is
  literally the same list of the same objects.
* **Why:** There is no broker-side order query in the sandbox build, so "actual" was wired to the
  same store as "expected" rather than left empty.
* **Failure scenario:** `_check_fills`, `_check_duplicates` and the order half of
  `_check_open_orders` compare a value to itself. A duplicate order, a missing fill or a partial
  fill can never be detected by reconciliation. Only `_check_positions`, which reads
  `broker.get_positions()`, is an independent comparison.
* **Expected:** "Actual" comes from the broker (or, in sandbox, from the broker's own simulated
  backend — `SimulatedSandboxTransport` exists precisely for this).
* **Actual:** Self-comparison.
* **Impact:** Reconciliation reports `matched: True` for fills by construction. The ADR-008 claim
  that "reconciliation is a kill switch" holds only for positions.
* **Confidence:** High.
* **Recommended fix:** Source `actual_orders` from `broker.get_order_status()` /
  `broker.get_open_orders()` across every known `internal_order_id`, falling back to the adapter's
  canonical broker view, and **raise** if the broker cannot enumerate its orders (fail closed)
  rather than substituting the local store. Patch in `FIX_PLAN.md`. **Not applied.**
* **Test required:** `test_reconciliation_detects_a_missing_fill` — inject a fill the broker
  knows about and the local store does not; reconciliation must lock.

---

## AUDIT-023 — CI is red at HEAD

* **Severity:** P1
* **Category:** CI/CD
* **File:** `.github/workflows/ci.yml`
* **Location:** lint + test steps
* **Problem (reproduced):**
  ```
  ruff check dashboard scripts tests/test_operational_dashboard.py
      tests/test_release_backup.py tests/test_release_dry_run.py
    -> Found 10 errors.
  ruff format --check <same paths>
    -> 2 files would be reformatted, 47 files already formatted
  pytest tests/ -m "not operational"
    -> 2 failed, 7 errors (AUDIT-004), plus the collection error (AUDIT-017)
  ```
  The workflow's `pytest … || pytest …` fallback does not help: both invocations fail.
* **Expected:** `main` is green.
* **Actual:** Three CI steps fail.
* **Impact:** A red pipeline trains everyone to ignore it, and (worse) hides real regressions.
* **Confidence:** High — all three commands were run.
* **Recommended fix:** `ruff check --fix` + `ruff format` on the CI-scoped paths (done — 10 errors
  fixed, 5 files reformatted; both commands now pass), and the test fixes from AUDIT-004/005/006.
* **Test required:** the CI job itself.

---

## AUDIT-035 — Out of the box, every paper action is blocked by stale data

* **Severity:** P1
* **Category:** Operational readiness
* **File:** `dashboard/strategy_dashboard.py`
* **Location:** `compute_momrem_signal` — `stale_days = (today - as_of.date()).days`,
  `"fresh": stale_days <= 5`
* **Problem:** The shipped clean bundle ends 2026-08-25. Today is 2026-09-01 → `stale_days = 7` →
  `fresh = False`. `_momrem_target()` then raises
  `daily signal is stale as of 2026-08-25; refresh validated EOD data first`.
* **Failure scenario (reproduced offline):** after working around AUDIT-013, the next wall is
  ```
  ValueError: daily signal is stale as of 2026-08-25; refresh validated EOD data first
  ```
  and over HTTP every paper action fails.
* **Expected:** Either the clone ships data fresh enough to exercise the workflow, or the first
  run tells the user exactly which command refreshes it.
* **Actual:** The workflow is blocked with no in-product recovery path (the suggested
  `python fetch_data.py` needs network access and an upstream source).
* **Impact:** A user who clones today cannot complete the documented daily workflow on day one.
  The fail-closed behaviour is *correct* — this is a packaging/freshness problem, not a safety
  one — but it is a functional dead end.
* **Confidence:** High — reproduced.
* **Recommended fix:** Ship a data-freshness banner with the exact refresh command, and document
  the expected data cadence. Not applied (content/data decision).

---

## AUDIT-032 — The research gate can be satisfied without the evidence it claims to require

* **Severity:** P1
* **Category:** Research validity
* **File:** `research/gate.py`
* **Location:** `ResearchGate.evaluate` — checks 2, 7, 8
* **Problem:** Three separate ways a weak strategy can clear the gate:
  1. `trials = config.tested_variants or (len(benchmarks) + len(placebo_results or {}) + 1)`.
     With the default `tested_variants=None` and no placebos supplied, `trials = 1`, and
     `expected_maximum_sharpe(1) = 0.0` — the Deflated Sharpe Ratio degenerates to an
     **uncorrected** single-trial Sharpe test while the docstring claims multiple-testing
     correction. Only `research/candidate_set.py` sets `tested_variants`.
  2. `validation=None` produces a `warn` check, so the verdict is `FRAGILE`, not `FAIL`. A
     strategy with **no walk-forward and no CPCV evidence at all** is one step from `PASS`.
  3. `evidence_returns = oos_returns if oos_returns is not None else returns` — when the caller
     omits `oos_returns` the gate silently grades the **in-sample** full backtest and reports it
     as "out-of-sample evidence".
* **Expected:** The gate is a barrier: no validation ⇒ no pass; DSR corrects for the number of
  strategies actually tried; "out-of-sample" is genuinely out-of-sample.
* **Actual:** All three are optional or under-counted.
* **Impact:** The barrier between research and paper capital is porous. A strategy selected after
  trying 200 variants and gate-checked with `trials=1` looks statistically sound.
* **Confidence:** High — read directly; `research/cli.py` and `dashboard/research_api.py` call
  `evaluate` without `oos_returns` in some paths.
* **Recommended fix:** Make `tested_variants` required (or fail closed when it is `None`),
  downgrade missing validation from `warn` to `fail`, and require `oos_returns` (or rename the
  check to `in_sample_evidence` in the output). Patch in `FIX_PLAN.md`. **Not applied** — it
  changes which strategies may reach paper trading, i.e. a gating policy decision.
* **Test required:** `test_gate_fails_without_validation_evidence`,
  `test_gate_dsr_counts_all_trials`.

---

## Remaining findings (P2/P3) — condensed

Each is expanded in `FORENSIC_AUDIT_REPORT.md` §6.

| ID | Sev | Finding | Evidence |
| --- | --- | --- | --- |
| AUDIT-024 | P2 | Two conflicting risk-limit systems: `risk_kill.RiskLimits` (max position 25%, gross 100%, daily loss 3%, drawdown 10%) and `paper_trading.DEFAULT_RISK_POLICY` (15% / 100% / 3% / **15%**). Which one governs is undefined. | both files |
| AUDIT-025 | P2 | `execution/state_machine.py` is a **0-byte file**. The documented order state machine does not exist as code; `PaperBroker` mutates a dict with no transition validation (FILLED → PENDING is representable). | `wc -c` |
| AUDIT-026 | P3 | `main.py`: `default=int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080` — index arithmetic on `sys.argv` inside a default; `IndexError` on a trailing `--port`. | read |
| AUDIT-027 | P2 | Running the test suite dirties committed files: `git status` after a run shows `M data/quant.duckdb` and `M data/snapshots/test_snap.parquet`. Tests write into the repository's committed data. | `git status --short` |
| AUDIT-028 | P2 | `/healthz` (the Docker/compose healthcheck) needs only the standard library, so the healthcheck reports **healthy** while every `/api/*` panel fails. Verified: with third-party imports blocked, `/healthz` would answer `ok` while `get_paper_service()` raises. | import-block experiment |
| AUDIT-029 | P2 | Staleness is **6 days** in `data.quality.validate_market_bars` and **18 hours** in `risk_kill.RiskLimits.max_data_age_hours`. The data layer tolerates data 8× older than the risk layer is willing to trade on. | both files |
| AUDIT-031 | P3 | Committed `execution/orders.jsonl` uses `price`, `expected_price`, `actual_price`; `OrderResult` defines `limit_price` and `average_fill_price`. The committed sample does not match the model it supposedly records. | both files |
| AUDIT-033 | P2 | `dashboard/main_dashboard.py`, `paper_dashboard.py`, `research_dashboard.py`, `broker_dashboard.py` import `streamlit`, which is in **neither** `dependencies` nor `dev`. `make dashboard` (`streamlit run dashboard/main_dashboard.py`) fails on a clean install. | grep + pyproject |
| AUDIT-034 | P2 | `/api/operations` reconciliation reports *"the signal expects 20 positions but the paper account is flat — run a paper rebalance (or enable auto-paper) to track it"*, while a rebalance is impossible (AUDIT-013) and the data is stale (AUDIT-035). The UI instructs the user to do something the system will refuse. | live HTTP |
| AUDIT-036 | P2 | `.env.example` documents `UPSTOX_API_KEY` and `UPSTOX_API_SECRET`, but `config/env_validator.validate_environment` **refuses to start** if either is set; and the validator defaults `SYSTEM_MODE` to `PAPER` (needs `DATABASE_URL`) while the example ships `LOCAL`, so a clean checkout fails its own preflight. | both files → **FIXED**: `SYSTEM_MODE` now defaults to `LOCAL`, and the example and `docs/secrets_management.md` state that the broker app credentials are fatal to the validator |
| AUDIT-037 | P2 | `docker-compose.yml` mounts `./data:/app/data:ro` while the application writes `data/quant.duckdb`; ingestion inside the container would fail. | compose + git status → **FIXED**: the mount is writable (`/api/data/rebuild-prices` rewrites `data/clean/prices.parquet`) |
| AUDIT-038 | P3 | `research/candidate_set.py` is the **only** caller that sets `ResearchGateConfig.tested_variants`; every other caller of `gate.evaluate` silently gets `trials=1` (AUDIT-032). | grep → **FIXED**: an undeclared trial count is now a `trial_count_declared` warning and `evidence_kind` records in-sample vs out-of-sample |
