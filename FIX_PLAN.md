# FIX PLAN

Audit date: 2026-09-01 · Base commit `80490c3`

Two categories, exactly as the brief requires:

* **SAFE TO AUTO-FIX** — applied in this change set, each with a regression test.
* **REQUIRES EXTRA CAUTION** — **not** applied. Each entry gives the precise patch and states
  the behavioural change it would cause, so the owner can decide. None of these is a
  cosmetic change; all of them move either a safety policy or a published number.

---

# PART A — SAFE TO AUTO-FIX (applied)

All of these are in the working tree and verified by the full suite
(`1403 passed, 5 skipped, 3 xfailed`, `ruff check` + `ruff format --check` clean).

| ID | Fix | File | Verification |
| --- | --- | --- | --- |
| AUDIT-001 | Add `risk_kill/mapping.py` (stdlib-only, total, fails closed) and use it in `ExecutionService.execute_targets` | `risk_kill/mapping.py` (new), `execution/service.py` | `test_audit_001_*` (3 tests, incl. end-to-end) |
| AUDIT-002 | Remove the FRED key; read `FRED_API_KEY` from the environment; raise `MissingCredential` | `scripts/ingest_macro.py` | `test_audit_002_*` (2 tests) |
| AUDIT-003 | Replace `\b` anchors with lookarounds + a value-shape heuristic | `tests/test_architecture.py` | `test_audit_003_*` |
| AUDIT-004 | Accept `--universe-dir` as an alias; skip absent indices (fail closed if *all* absent); `merge_membership_audit()`; `UniverseDataset.from_dir` descends **only as a fallback**; `_resolve_universe_dir()` in the experiment runner | `scripts/ingest_real_data.py`, `scripts/run_real_data_experiment.py`, `data/universe.py` | `test_audit_004_*` (4 tests) + 21 real-data tests now run |
| AUDIT-005 | Validate the OHLCV header prefix; tolerate both upstream trailing-column dialects | `ingestion/eod2_adapter.py` | `test_audit_005_*` (2 tests) |
| AUDIT-006 | Thread `as_of` through `validate_market_bars`; default `run_day`'s as-of to **today in IST** | `data/quality.py`, `orchestration/pipeline.py` | `test_audit_006_*` (2 tests) |
| AUDIT-015 | `preview_rebalance` uses the `QuoteChain`; fill `source` derived from the real quote source; expose `quote_sources` | `paper_trading/service.py` | existing suite + 3 new gap tests specified |
| AUDIT-016 | Add `"data"` to `setuptools.packages` | `pyproject.toml` | `test_audit_016_*` |
| AUDIT-017 | Declare `seaborn>=0.13.0` | `pyproject.toml` | `test_audit_017_*` |
| AUDIT-018 | `pip install --no-cache-dir .` (with dependencies) | `Dockerfile` | `test_audit_018_*` |
| AUDIT-019 | `./var:/app/var` read-write; tmpfs for the runtime HOME | `docker-compose.yml` | `test_audit_019_*` |
| AUDIT-020 | New `scripts/preflight.py` that actually calls `validate_environment` (optional `--db`, `--json`, non-zero exit) | `scripts/preflight.py` (new) | `test_audit_020_*` (2 tests) |
| AUDIT-023 | `ruff check --fix` + `ruff format` on the CI-scoped paths | `dashboard/`, `scripts/`, 3 test files | both CI lint commands now pass |
| — | New `tests/test_forensic_audit_regressions.py` (26 tests) | `tests/` | all pass |

### Note on AUDIT-001 — why `risk_kill/mapping.py` returns a *name*

`tests/test_architecture.py` enforces (by AST walk) that `risk_kill` imports nothing outside
the standard library. `SystemHealth` lives in `observability.health`, so `risk_kill` must not
import it. `mapping.health_name_for_risk_state()` therefore returns the member **name**
(`"HALTED"`, `"LOCKED"`, …) and `execution/service.py` does `SystemHealth(name)`.
`execution` already imports `observability`, so the boundary is respected.

### Note on AUDIT-020 — deliberately *not* wired into server startup

`validate_environment()` treats `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` / `DHAN_CLIENT_ID` as
fatal, while `.env.example` documents them. Calling it from `dashboard/server.py` would break
every existing deployment that follows the documented setup. A standalone `preflight.py` gives
the safety property without the blast radius. The underlying contradiction is AUDIT-036.

### Note on AUDIT-004 — `from_dir` descends only as a fallback

The repository's `data/universe` holds **both** flat CSVs (`nifty100.csv`, …) and
`<slug>-pit/` directories, and the pit directories contain stray copies
(`nifty50-pit/nifty100.csv`). Greedy recursion double-counted the universe and broke
`tests/test_baseline_experiment.py`. The fix: files directly inside the directory always win;
sub-directories are consulted **only** when no CSV/Parquet exists at the top level.

---

# PART B — REQUIRES EXTRA CAUTION (proposed, not applied)

Ordered by severity. Each entry: the patch, then the behavioural change it causes.

---

## B1. AUDIT-021 — Make the kill switch authoritative (P0)

**Problem:** `datahub.state` (the persisted operator switch) is not consulted by
`execution/service.py` or `orchestration/pipeline.py`. `RiskGuard` has no persistence.

**Patch:**

1. New `datahub/kill_switch.py` exposing `is_killed()` and `require_not_killed(action)`, both
   reading `datahub.state` (single authority).
2. In `ExecutionService.execute_targets`, immediately after entering the lock and **before**
   `self.risk_guard.evaluate(...)`:
   ```python
   from datahub.kill_switch import require_not_killed
   if require_not_killed("execute_targets"):
       return ExecutionSummary(
           run_id=run_id,
           risk_state=RiskState.LOCK_ACCOUNT.value,
           submitted=[], skipped=[],
           halted=True,
           halt_reason="operator kill switch is armed",
       )
   ```
   (plus the same `health_service.set_state(SystemHealth.LOCKED, …)` and
   `alert_service.critical(…)` as the existing protective branch).
3. Same check as step 0 of `DailyPipeline.run_day`, returning
   `DailyRunResult(status="halted_kill_switch", …)`.
4. Persist `RiskGuard` outcomes: after `evaluate`, if the state is not `NOMINAL`, write it to
   `datahub.state` as `heartbeats.risk_state` so a restart sees the last protective state.
   **Delete `risk_kill/state.json`** — it is read by nothing and implies a layer that does not
   exist.
5. `scripts/run_daily.py` should refuse to start while the switch is armed.

**Behavioural change:** arming the kill switch will now stop **orchestrated and execution**
order flow, not just the paper service and the demo bot. Anyone who currently arms the switch
*and* runs `scripts/run_daily.py` expecting it to trade will find it does not. That is the
intended semantics (the UI already says "ARM"), but it must be announced.

**Tests to land first:** G1, G2, G3 in `TEST_GAP_ANALYSIS.md`.

---

## B2. AUDIT-030 — Give the risk context real inputs (P0)

**Problem:** `DailyPipeline._risk_context` hardcodes `equity_day_start = equity_now`,
`broker_connected = True`, `order_timestamps = ()`, and seeds `equity_peak` from the first
equity this process observes.

**Patch:**

1. Add an `equity_snapshots(date, equity, cash, market_value, recorded_at)` table to the
   persistence layer; write one row on every mark-to-market.
2. ```python
   history = self.equity_repository.history(limit=2)
   equity_day_start = float(history[0].equity) if history else equity   # previous snapshot
   equity_peak      = max(self._equity_peak or equity,
                          *(row.equity for row in history))             # across restarts
   ```
3. `broker_connected`: derive from a real heartbeat with a freshness bound —
   ```python
   beat = self.health_service.last_broker_ping()      # or datahub.state heartbeat
   broker_connected = bool(beat and beat.age_seconds <= 300)
   ```
   and **leave it `None`** when unknown, because `RiskGuard.check_broker_connectivity` already
   maps `None → LOCK_ACCOUNT` (fail closed).
4. `order_timestamps`: pass the timestamps of orders actually submitted in the last hour, read
   from the order repository.
5. If any input is unavailable, log a `risk_context_incomplete` warning and leave the field
   `None` — never substitute a benign value.

**Behavioural change:** the daily-loss, broker-connectivity and order-rate checks will start
firing. A deployment that has been running "NOMINAL" may start halting. That is the point, but
it must be rolled out with alerting in place, and the persisted equity history needs
back-filling first or the first run will see `equity_day_start = equity` anyway.

**Tests:** G4, G5.

---

## B3. AUDIT-014 — Stop fabricating pre-listing prices (P0)

**Problem:** `research/realdata.py::build_market_panels` keeps incomplete-history symbols in
the panel while listing them in `excluded`, and `_pivot_field(...).ffill().bfill()` copies a
symbol's first traded price backwards over every date before it existed.

**Two-stage patch:**

*Stage 1 — honesty (no number changes). Already applied in this change set:* the docstring is
corrected, `ResearchPanels` gains `incomplete_symbols` and `price_fill`, and both behaviours are
opt-in via `exclude_incomplete` / `fill_missing_prices` (defaults preserve history).

*Stage 2 — correctness (changes every published number). Proposed:*

```python
def build_market_panels(..., exclude_incomplete: bool = True,
                              fill_missing_prices: bool = False):
```
and update the four callers (`scripts/ingest_real_data.py`,
`scripts/run_real_data_experiment.py`, `scripts/run_research_experiment.py`,
`dashboard/api.py`) to pass explicit values.

**Behavioural change:** the panel shrinks (every symbol with any missing session drops out),
momentum and volatility inputs change, and **every published Sharpe/CAGR/drawdown changes**.
The three `xfail(strict=True)` tests in `tests/test_real_data_pipeline.py` will flip to
`XPASS` and fail the build — that is the designed forcing function; un-mark them as part of the
same change.

**Order of operations:** (1) land the flags; (2) re-run the frozen v0.6 baseline and the v0.7
experiment; (3) record the new numbers and re-run the research gate; (4) flip the defaults;
(5) un-mark the xfails. Do not ship (4) without (3).

**Tests:** the three xfails, plus G10.

---

## B4. AUDIT-007 — Implement the survivorship-bias guard (P1)

**Problem:** `VectorBTResearchEngine.run` requires `universe_history` and then ignores it.

**Patch:**

```python
def run(self, prices, target_weights, strategy_name="strategy",
        universe_history=None):
    ...
    members = _coerce_membership(universe_history, index=prices.index,
                                 columns=prices.columns)
    if members is None:
        raise ResearchInputError(...)            # keep the current refusal
    prices  = prices.where(members)
    weights = target_weights.where(members)
```
`_coerce_membership` accepts either a boolean `pd.DataFrame` aligned to
`(prices.index, prices.columns)` or a sequence of `(date, frozenset[symbol])` pairs; it must
reject an empty sequence with `ResearchInputError` (today `universe_history=[]` is accepted and
means "no protection").

**Behavioural change:** every caller that currently passes `[]` —
`generate_placebo_results`, `backtest/validation._evaluate_windows`, and the experiment scripts
— will start raising. They must be threaded with the real PIT mask, which
`research.realdata.build_active_membership_panel` already produces and already passes into
`MomentumQualityStrategy(active_members=mask)`. Once wired, the panel is masked per date and
published numbers change (they will get *worse* — which is the honest direction).

**Tests:** G11.

---

## B5. AUDIT-008 / AUDIT-009 — One execution model per backtest (P1)

**Problem:** returns come from VectorBT while turnover, cost and trade count always come from
`_simulate_pandas`; and `_validate_inputs` silently `ffill().bfill()`s the price panel.

**Patch (recommended, smallest blast radius):**

1. Make the **pandas** path authoritative for reporting: keep VectorBT as a cross-check only,
   and stamp `metadata["backend"] = "pandas"` for every result used in a gate decision.
   Log a warning whenever `backend == "vectorbt"` is used for a gate decision.
2. If VectorBT results are still wanted, compute the trade table from the VectorBT portfolio
   (`portfolio.trades.records_readable`) so costs reconcile with the returns.
3. Replace `_validate_inputs`'s unconditional fill with:
   ```python
   if numeric_prices.isna().any().any():
       raise ResearchInputError(
           "prices contain gaps; the system does not impute prices "
           "(see data.quality) — mask or exclude the affected symbols first")
   ```
   and add an explicit `allow_price_fill: bool = False` escape hatch.

**Behavioural change:** results become reproducible across machines (no more silent
vectorbt-import dependence); `cost_drag` reconciles with `total_return`; backtests on panels
with gaps will start **raising** instead of silently producing numbers. Step 3 in particular
will break any caller that currently relies on the fill — the clean bundle has gaps
(AUDIT-010 measured 75 missing candles in 20 symbols), so this must land together with B6.

**Tests:** G8, G9, G10.

---

## B6. AUDIT-010 — Classify data-quality issues as blocking vs advisory (P1)

**Problem:** `run_day` halts on **any** issue; `detect_missing_candles` emits one issue per
(symbol, date) gap. Measured: 137 issues across 20 clean symbols with **zero rows rejected**.

**Patch:**

```python
BLOCKING_KINDS = frozenset({
    "invalid_timestamp", "invalid_symbol", "duplicate_row",
    "future_date", "non_positive_price", "ohlc_inconsistent",
    "staleness", "empty_after_validation",
})
ADVISORY_KINDS = frozenset({"missing_candle", "off_calendar"})

blocking = [i for i in report.issues if i.kind in BLOCKING_KINDS]
if accepted.empty or blocking:
    -> halted_data_quality
# advisory issues are counted, capped in the report, and logged
```

**Behavioural change:** the daily run will proceed on real data where it previously halted
every single day. **This is a deliberate relaxation of a safety control and must not be merged
without a matching alert.** Two compensating controls are required in the same change:

1. A configurable cap — e.g. halt when advisory issues exceed 5% of
   `(symbols × sessions)` — with the cap recorded in the run record.
2. A `data_quality_advisory` **warning** alert every run that has any advisory issue, so the
   degradation is never silent.

**Tests:** G6 (must not halt) and G7 (must still halt on real invalid rows).

---

## B7. AUDIT-022 — Make reconciliation compare against the broker (P1)

**Problem:** `_expected_state()` and `_all_broker_orders()` both read `self.order_repository`,
so `_check_fills`, `_check_duplicates` and the order half of `_check_open_orders` are
tautological.

**Patch:** in `orchestration/pipeline.py`:

```python
def _all_broker_orders(self) -> list:
    """Orders as the *broker* reports them. Never sourced from the local store."""
    orders = []
    for intent in self.order_repository.list_intents():
        record = self.broker.get_order_status(intent.internal_order_id)
        if record is not None:
            orders.append(record)
    return orders
```
and add a coverage check: if the broker cannot enumerate an order the local store believes was
submitted, raise `ReconciliationError` (fail closed) rather than silently substituting the
stored result. `SandboxExecutionAdapter` already implements `get_order_status`, and
`SimulatedBrokerBackend` is an independent source, so the plumbing exists.

**Behavioural change:** reconciliation will start detecting real mismatches — including any
that exist today and are currently invisible. Expect the first runs to lock the account. That
is the correct outcome for a system whose ADR-008 says "reconciliation is a kill switch", but
roll it out in paper mode first.

**Test:** G14.

---

## B8. AUDIT-011 / AUDIT-012 — Indian-market data correctness (P1)

**AUDIT-011 (calendar).** Load a maintained NSE trading calendar into
`data/quality.py::TradingCalendar`:
```python
TradingCalendar(holidays=set_of_dates, special_sessions=set_of_dates, weekend={5, 6})
```
Source it from NSE's published exchange-holiday circular (authoritative, updated annually) and
commit it as a dated data file with a `source` and `retrieved_at` in the provenance manifest, in
the same style as the rest of the repository. Update `nse_weekday_calendar()` callers to use it.

*Behavioural change:* `detect_off_calendar_candles` stops flagging legitimate Budget special
sessions, and `detect_missing_candles` stops expecting trading on Diwali. Both the false
positives and the false negatives disappear. Annualisation and staleness change slightly.

**AUDIT-012 (series).** In `ingestion/eod2_adapter.py::parse_eod2_daily_file`, filter to
`Series == "EQ"` and record the dropped series and row counts in the provenance manifest:
```python
non_eq = frame[frame["series"].astype(str).str.strip().str.upper() != "EQ"]
frame = frame.drop(non_eq.index)
```
Measured impact: ~8% of rows, across 377 of 600 sampled symbols.

*Behavioural change:* price history for two thirds of the universe changes (artificial jumps at
series transitions disappear), which changes every downstream number. This must be re-baselined
together with B3, not separately.

**Tests:** G12, G13.

---

## B9. AUDIT-032 / AUDIT-038 — Tighten the research gate (P1)

**Patch** (`research/gate.py`):

1. `ResearchGateConfig.tested_variants: int` — make it **required** (no `None` default), or
   fail closed: `if config.tested_variants is None: raise ResearchInputError(...)`.
2. `validation is None` ⇒ `GateCheck(status="fail", …)` instead of `"warn"`.
3. `oos_returns is None` ⇒ raise, or rename the reported field to `in_sample_evidence` and add
   `check.name = "in_sample_evidence"` so the report cannot be misread.
4. Add a `min_trade_count` check (e.g. ≥ 30 trades) to `ResearchGateConfig`.

**Behavioural change:** strategies that currently reach `FRAGILE` or `PASS` will drop to `FAIL`
if they have no walk-forward/CPCV evidence or if the caller does not pass `oos_returns`. This
can only make the barrier stricter — no strategy gains approval — which is the safe direction,
but it will change the gate output for existing experiments.

**Tests:** G18, G19.

---

## B10. AUDIT-013 / AUDIT-034 — Paper-trading reachability, without weakening the gate (P1/P2)

**Explicitly NOT proposed:** flipping `paper_approved` to `true` to make the button work. That
is the failure mode the brief warns against.

**Proposed:**

1. Add `momrem` to `config/paper_strategies.json` with an explicit `reason` string so the file
   and `DEFAULT_REGISTRY` agree (the discrepancy, not the value, is the bug).
2. Surface `rebalance_blocked_reason` in `PaperTradingService.status()`:
   ```python
   "rebalance": {
       "blocked": True/False,
       "reason": "strategy is not paper-approved by the research gate" | 
                 "daily signal is stale as of 2026-08-25" | 
                 "UPSTOX_ACCESS_TOKEN is not configured" | None,
       "next_action": "...",
   }
   ```
   and render it in the UI next to a **disabled** Rebalance button.
3. In `dashboard/operations.py::reconciliation`, replace the unconditional
   *"run a paper rebalance …"* string with the actual blocking reason, or with a neutral
   *"paper account is flat"* when a rebalance is not currently possible.
4. Document the approval procedure (gate `PASS` → registry entry → reason) in the runbook.

**Behavioural change:** none to safety. The UI stops inviting an action the system will refuse.

**Tests:** G15.

---

## B12. AUDIT-039 — Put a control in front of the dashboard (P0)

**Problem:** `dashboard/server.py` binds `0.0.0.0` with no authentication, authorisation, CSRF
or origin check on any route. An unauthenticated `POST /api/kill-switch {"armed": false}`
disarms the operator kill switch.

**Patch (minimum viable, in order of preference):**

1. **Bind to loopback.** Change `run_server` to `("127.0.0.1", actual_port)` by default and
   require an explicit `QUANT_DASHBOARD_BIND=0.0.0.0` opt-in. The existing `# nosec B104`
   suppression is only defensible on loopback. Put a reverse proxy with authentication in
   front for any non-local deployment.
2. **Shared-secret header on every mutating route.** In `do_POST`, before dispatch:
   ```python
   expected = os.getenv("QUANT_DASHBOARD_TOKEN", "")
   if not expected or not hmac.compare_digest(
           self.headers.get("X-Quant-Token", ""), expected):
       self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
       return
   ```
   Refuse to start if the server is bound to a non-loopback address and
   `QUANT_DASHBOARD_TOKEN` is unset — i.e. **fail closed**, in the same style as the rest of
   the repository.
3. **Origin check + CSRF token** for browser-initiated state changes.

**Behavioural change:** every existing deployment that hits the dashboard directly will need a
token (or to bind to loopback). This will break unattended scripts that `POST` to the API.
Announce it and provide the token in the runbook. Note this is the **only** Part B item that
makes the system *stricter without changing any trading outcome* — it should probably be the
first one merged.

**Test:** `test_dashboard_mutating_routes_require_auth` — unauthenticated POST to
`/api/kill-switch`, `/api/paper/reset`, `/api/paper/rebalance`, `/api/paper/automation`,
`/api/data/rebuild-prices`, `/api/universe/expand`, `/api/research/run`, `/api/live/bot`,
`/api/signal/recompute`, `/api/paper/*` must all return 401/403.

---

## B11. AUDIT-024 / AUDIT-025 / AUDIT-027 / AUDIT-028 / AUDIT-033 / AUDIT-036 (P2/P3)

| ID | Patch | Behavioural change |
| --- | --- | --- |
| AUDIT-024 | Pick one authority for risk limits. Recommended: `risk_kill.RiskLimits` is the ceiling; `paper_trading` policy is validated **against** it on `set_risk_policy` and rejected if looser. | Some currently-accepted paper policies will be rejected. Stricter, safe direction. |
| AUDIT-025 | Fill `execution/state_machine.py` with `ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]]` and a single `apply_transition()` used by every writer (`PaperBroker`, `SimulatedBrokerBackend`, adapters). | Illegal transitions that nothing currently performs will start raising. |
| AUDIT-026 | Replace the `sys.argv` index arithmetic in `main.py` with a plain `default=8080, type=int`. | None. |
| AUDIT-027 | Point tests at `tmp_path` / a `QUANT_DATA_DIR` fixture instead of the committed `data/`; add `data/quant.duckdb` and `data/snapshots/` to `.gitignore` if they are genuinely derived. | Test runs stop dirtying the tree. Requires confirming nothing *reads* the committed duckdb at import time. |
| AUDIT-028 | Extend `/healthz` to touch the data layer (e.g. `datahub.panel.data_status()`), so the Docker/compose healthcheck reflects reality. | The healthcheck will fail when dependencies are missing — which is the point, and will surface AUDIT-018-class breakage immediately instead of crash-looping later. |
| AUDIT-029 | Derive all three staleness thresholds from one `QUANT_MAX_DATA_AGE_HOURS` setting; keep the strictest (18 h) as the default for anything that can trade. | Looser paths (6-day data validation, 5-day signal freshness) will start refusing to trade sooner. |
| AUDIT-033 | Add `streamlit` to `[project.optional-dependencies].dashboards`, or delete the four Streamlit dashboards. `make dashboard` currently fails on a clean install. | None if added as an extra. |
| AUDIT-036 | Reconcile `.env.example` with `config/env_validator`: either stop treating `UPSTOX_API_KEY`/`UPSTOX_API_SECRET` as fatal, or remove them from the example and document that they must never be set. Also align the default `SYSTEM_MODE` (validator: `PAPER`; example: `LOCAL`) — as it stands a clean checkout with no `.env` fails its own preflight because `PAPER` requires `DATABASE_URL`. | Determines whether the documented setup is legal. Must be an explicit product decision. |
| AUDIT-037 | Mount `./data:/app/data` read-write, or make the application never write to `data/`. Currently the compose mount is `:ro` while the app writes `data/quant.duckdb` (AUDIT-027). | Ingestion inside the container starts working. |
| AUDIT-031 | Regenerate or delete `execution/orders.jsonl`; its keys (`price`, `expected_price`, `actual_price`) do not match `OrderResult` (`limit_price`, `average_fill_price`). | None (sample data). |

---

# PART C — IMMEDIATE ACTIONS FOR THE OWNER (outside the code)

1. **Rotate the FRED API key.** It is in git history at commit `80490c3`, which has been
   pushed. Removing it from the working tree does not un-publish it.
   → https://fred.stlouisfed.org/docs/api/api_key.html
2. **Audit for abuse** of the same key before rotating, if the account has usage logs.
3. **Decide on B3 and B4 together.** They are the two changes that determine whether the
   published research numbers mean anything. Until both land, every backtest produced by this
   repository should be treated as indicative, not as evidence.
4. **Do not connect a broker.** There is no live order path, and that is currently the
   system's most valuable property. Adding one before B1, B2 and B7 land would remove the only
   thing standing between a bad signal and real money.
