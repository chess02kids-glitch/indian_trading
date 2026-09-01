# END-TO-END EXECUTION TRACES

Audit date: 2026-09-01 · Base commit `80490c3`
Method: every trace below was **executed**, not read. Commands, actual output and
observations are recorded verbatim. Where a step failed, the failure is the result.

Environment: fresh `.venv` (Python 3.11.2), `pip install -e .`, plus `seaborn`,
`pytest-mock`, `hypothesis`, `pytest-asyncio`, `pytest-timeout`, `ruff`.
No broker credentials were configured at any point. **No order was ever sent to a broker.**

---

## Trace 0 — Clean install and test collection

| Step | Command | Result |
| --- | --- | --- |
| 0.1 | `python -m venv .venv && pip install -e .` | OK (pulls yfinance, duckdb, supabase, vectorbt, scikit-learn, pydantic, pandera) |
| 0.2 | `pytest tests/ -q` | **COLLECTION ERROR** — `dashboard/strategy_performance.py:43: ModuleNotFoundError: No module named 'seaborn'` → AUDIT-017 |
| 0.3 | `pip install seaborn pytest-mock hypothesis pytest-asyncio` | OK |
| 0.4 | `pytest tests/ -q --co` | 1264 tests collected, 4 warnings |
| 0.5 | `pytest tests/ -q` (full) | **1250 passed, 2 failed, 5 skipped, 7 errors, 12 warnings in 145.49 s** |

Baseline result at HEAD:

```
FAILED tests/test_real_data_pipeline.py::test_adapter_missing_field_raises
FAILED tests/test_real_data_pipeline.py::test_deterministic_snapshot
ERROR  tests/test_real_data_pipeline.py::test_universe_constituent_validation
ERROR  tests/test_real_data_pipeline.py::test_missing_constituent_handling
ERROR  tests/test_real_data_pipeline.py::test_universe_version_fingerprinting
ERROR  tests/test_real_data_pipeline.py::test_adjustment_state_validation
ERROR  tests/test_real_data_pipeline.py::test_symbol_continuity_isin
ERROR  tests/test_real_data_pipeline.py::test_ingestion_to_research_integration
ERROR  tests/test_real_data_pipeline.py::test_full_real_data_pipeline_ledger_and_reproducibility
```

CI at HEAD (all three commands run locally):

```
ruff check  dashboard scripts tests/test_operational_dashboard.py
            tests/test_release_backup.py tests/test_release_dry_run.py
  -> Found 10 errors.
ruff format --check  <same paths>   -> 2 files would be reformatted
pytest tests/ -m "not operational"  -> 2 failed, 7 errors
```
⇒ **CI is red at HEAD** (AUDIT-023).

---

## Trace 1 — The one-trade flow (orchestrated daily run)

Path: `scripts/run_daily.py` → `orchestration.DailyPipeline.run_day`
→ data validation → signals → constructor → risk guard → approval gate
→ `ExecutionService.execute_targets` → broker → reconciliation.

### 1.1 Data validation on real data — halts

```
rows: 11504 symbols: 20 dates: 2024-01-01 -> 2026-08-25
total_rows 11504  accepted 11504
issues by kind: {'staleness': 1, 'missing_candle': 75, 'off_calendar': 61}
is_clean: False
```

`run_day` step 1 is `if accepted.empty or report.issues: → halted_data_quality`.
**Zero rows were rejected, yet the day halts.** Extrapolated to the 133-symbol clean
bundle this is thousands of issues. ⇒ AUDIT-010.

### 1.2 Calendar reality

```
off_calendar sample: ('ABB','2024-01-20') ('ABB','2024-03-02') ('ABB','2024-05-18')
                     ('ABB','2025-02-01') ('ABB','2026-02-01')
weekday distribution: {'Saturday': 50, 'Sunday': 11}
```
2025-02-01 and 2026-02-01 are **real NSE Union Budget special sessions**, flagged as errors.
NSE holidays are never detected because the calendar is weekday-only. ⇒ AUDIT-011.

### 1.3 The risk-halt path crashes

Reproduced directly (pre-fix):

```python
ctx   = RiskContext(..., data_last_updated=datetime(2026,8,1))   # stale
dec   = RiskGuard().evaluate(ctx)
     -> RiskDecision(state=STOP_NEW_ORDERS, triggered_by=('data_staleness',))
svc.execute_targets(target, run_id=..., reference_prices=..., risk_context=ctx)
     -> EXCEPTION: AttributeError  HALTED
```

⇒ AUDIT-001. Post-fix the same call returns
`ExecutionSummary(run_id=..., risk_state='STOP_NEW_ORDERS', submitted=[], skipped=[], halted=True)`.

### 1.4 Risk checks that can never fire

```python
RiskContext(
    equity_now=equity,
    equity_day_start=equity,   # identical ⇒ check_daily_loss always 0
    equity_peak=self._equity_peak,   # seeded from this process's first equity
    broker_connected=True,     # ⇒ check_broker_connectivity never trips
    order_timestamps=(),       # ⇒ check_order_rate never trips
)
```
⇒ AUDIT-030.

**Verdict for Trace 1: the orchestrated one-trade flow cannot complete on real data.**
It halts at step 1 on a clean 20-symbol dataset; if it got past that, three risk checks
are inert; and if a check did fire, the halt path crashed.

---

## Trace 2 — The bad-trade flow (protective risk decision)

| Attempt | Expected | Actual (pre-fix) | Actual (post-fix) |
| --- | --- | --- | --- |
| Stale data (8 days old) | `STOP_NEW_ORDERS`, halt, no order submitted | `AttributeError: HALTED` | `halted=True`, `submitted=[]`, health=HALTED, critical alert raised |
| Reconciliation locked | `LOCK_ACCOUNT`, halt | `AttributeError: HALTED` (same code path) | `halted=True`, health=LOCKED |
| Drawdown breach | `FLATTEN_POSITIONS`, halt | `AttributeError: HALTED` | `halted=True`, health=HALTED |

Broker stub asserts it is never touched: `submit_order` raises `AssertionError` if called.
It is never called. ⇒ AUDIT-001 fixed and verified.

---

## Trace 3 — The duplicate-trade flow (idempotency)

`execution/idempotency.py::compute_idempotency_key` covers
`strategy / hypothesis / symbol / side / quantity / limit_price / order_type / rebalance_date`.
`ExecutionService` calls `IdempotencyRegistry.claim` before `broker.submit_order`, and
`SandboxExecutionAdapter` returns the stored result for a repeated key without contacting the
broker. `reconciliation/engine.py::_check_duplicates` flags two orders sharing a key.

**But:** in `orchestration/pipeline.py`, `actual_orders` (`_all_broker_orders`, line 626) and
`expected` (`_expected_state`, line 301) both read `self.order_repository`. The duplicate check
compares the repository to itself and can never fire. ⇒ AUDIT-022.

The idempotency mechanism itself is sound; the reconciliation that is supposed to *verify* it
is tautological.

---

## Trace 4 — The paper-trading flow (the product's headline workflow)

Executed against a running server (`python dashboard/server.py`, port 8080) and offline.

### 4.1 Via HTTP

```
POST /api/paper/start                                     -> 200 (settings.running = true)
POST /api/paper/preview    {"strategy_id":"momrem"}
     -> {"error": "strategy is not paper-approved by the research gate"}
POST /api/paper/rebalance  {"strategy_id":"momrem","confirmation":"PAPER REBALANCE"}
     -> {"error": "strategy is not paper-approved by the research gate"}
```

Cause: `config/paper_strategies.json` — 30 strategies, **all** `paper_approved: false`, and
`momrem` (the only strategy with a registered target builder) is **not in the file at all**.
⇒ AUDIT-013.

### 4.2 Offline, with the registry workaround applied

Next wall:
```
ValueError: daily signal is stale as of 2026-08-25; refresh validated EOD data first
```
`stale_days = (2026-09-01 - 2026-08-25).days = 7 > 5` ⇒ `fresh = False`.
⇒ AUDIT-035.

### 4.3 Offline, with both workarounds applied

Next wall:
```
ready: False
reason: configure UPSTOX_ACCESS_TOKEN; API key/secret alone cannot fetch quotes
```
`preview_rebalance` called `UpstoxMarketData.fetch_quotes` directly, bypassing the
UPSTOX→SIM→EOD `QuoteChain` that `refresh_quotes` uses — so the dashboard tape renders
SIM quotes happily while the only order-creating path hard-fails. ⇒ AUDIT-015 (fixed).

### 4.4 Read model — traceable, with one misleading message

```
GET /api/operations
  broker_health.configured = false, state = NOT_CONFIGURED,
                             detail = "UPSTOX_ACCESS_TOKEN is not configured"
  broker_health.last_quote_detail = {error: null, missing: [], quoted: 5,
                                     requested: 5, source: "UPSTOX"}   <-- misleading
  reconciliation.detail = "the signal expects 20 positions but the paper account
                           is flat — run a paper rebalance (or enable auto-paper)
                           to track it"                                <-- impossible
  kill_switch = {armed: false}
```
`source: "UPSTOX"` is the *configured mode* string, not the source that priced the quotes — the
chain actually served SIM. And the reconciliation text instructs the user to do the one thing
the system currently refuses. ⇒ AUDIT-034.

**Verdict for Trace 4: the paper-trading workflow has three independent hard stops
(AUDIT-013 → AUDIT-035 → AUDIT-015) before a single virtual order can be created.**
Each is fail-closed, which is the right *direction*, but the product is not exercisable
end-to-end as shipped.

---

## Trace 5 — Stale data

* `data.quality.detect_data_staleness` default: **6 days**.
* `risk_kill.RiskLimits.max_data_age_hours`: **18 hours**.
* `dashboard.strategy_dashboard` freshness: **5 days**.

Three thresholds, an 8× spread between the strictest and the loosest, none reconciled.
⇒ AUDIT-029.

`RiskGuard.check_data_staleness` itself fails closed correctly (`data_last_updated=None`
⇒ `LOCK_ACCOUNT`, verified by reading the guard). The problem is which threshold reaches it.

---

## Trace 6 — Token expiry

* `broker/token.py::TokenRecord.seconds_until_expiry` and `is_expired` exist.
* `broker/safe_execution.py` step 6: `TokenManager.get_token` raises `StaleTokenError`, which is
  converted into a deterministic `REJECTED` order result **before** any transport call.
* `dashboard/operations.py::broker_health` reads the stored token and reports
  `state = TOKEN_EXPIRED`.

Observed live with no token configured: `state: "NOT_CONFIGURED"`, `token: null`.
The token-lifecycle handling is **correct** and is one of the strongest parts of the system.
No defect found.

---

## Trace 7 — Restart / disaster recovery

| State | Persisted? | Survives restart? | Evidence |
| --- | --- | --- | --- |
| Operator kill switch (`datahub.state`) | Yes — `var/system_state.json`, atomic write, 200-entry history | **Yes** | `datahub/state.py::_write` |
| Deterministic guard state (`risk_kill`) | **No** | **No** | `RiskGuard` is a pure function; no writer |
| `risk_kill/state.json` | Ships hand-written | n/a — **no code reads it** | grep: only `dashboard/main_dashboard.py` + `scripts/generate_sample_data.py` |
| Paper ledger | Yes — SQLite | Yes | `paper_trading/ledger.py` |
| Idempotency claims | Via the order repository | Depends on the repository (Supabase in prod) | `execution/idempotency.py` |
| Equity peak for the drawdown check | **No** | **No** | `DailyPipeline._equity_peak` is an instance attribute |

⇒ AUDIT-021 (kill switch not persisted / not consulted by execution) and AUDIT-030
(drawdown peak resets on restart, so a 9% drawdown is forgotten after a reboot).

---

## Trace 8 — Broker connectivity (no live path exists — verified)

* `broker/transport.py` ships `SimulatedSandboxTransport` (in-process) and
  `HttpSandboxTransportStub`, which **validates that its URL is a sandbox endpoint and then
  refuses to perform the request** because no HTTP client is wired in.
* `validate_sandbox_base_url` permits only `simulated://`, loopback, and `sandbox.*` hosts.
* `BaseSandboxAdapter.__init__` raises `LiveTradingDisabledError` on `OperatingMode.LIVE`.
* `grep -rn "requests\|httpx\|urllib\|socket\|urlopen"` over `broker/`, `paper_trading/`,
  `dashboard/live/`, `datahub/` and `dashboard/` finds **only** `urllib.parse.unquote` in
  `dashboard/server.py`.

**Conclusion: the README's central claim is true.** There is no code path to a broker order
API. `modelss/domain.py::ExecutionMode` has no `LIVE` member, `OrderType` has only `LIMIT`, and
`tests/test_architecture.py` enforces all of this in code. This is a genuine strength.

---

## Trace 9 — Deployment

Docker is not available in this sandbox, so the image was not built. Instead the exact failure
was reproduced by blocking third-party imports and starting the entry point:

```
get_paper_service FAILED: ImportError BLOCKED third-party import: numpy
/api/overview    FAILED: ImportError BLOCKED third-party import: numpy
/api/operations  FAILED: ImportError BLOCKED third-party import: numpy
```

`run_server()` calls `get_paper_service()` **before** binding the port ⇒ the container exits
immediately ⇒ crash loop. Cause: `pip install --no-deps .`. ⇒ AUDIT-018 (fixed).

Separately, the compose file mounted `./var:/app/var:ro` while the kill switch is written to
`var/system_state.json` ⇒ `POST /api/kill-switch` would 503. ⇒ AUDIT-019 (fixed).

And `/healthz` needs only the standard library, so the healthcheck reports healthy even when
every data panel fails. ⇒ AUDIT-028.

---

## Trace 10 — Real-data ingestion (post-fix)

After AUDIT-004 was fixed, the previously-dead fixture runs. Progression:

```
before:  2 failed, 7 errors, 13 passed     (9 tests never executed)
after :  21 passed, 3 xfailed
```

The three `xfail(strict=True)` tests are AUDIT-014: they encode the documented contract
(incomplete-history symbols are excluded) and fail because the code keeps and back-fills them.

The panel defect, measured on the fixture world:

```
requested: 53   panel symbols: 52
NEWCO in panel symbols: True
panels.excluded["NEWCO"] = "incomplete price history in window: 306 calendar day(s)
                            missing (first observation 2024-03-05)"
NEWCO clean first date: 2024-03-05        panel first date: 2023-01-02
NEWCO close rows before its first real observation: 306    NaNs: 0
NEWCO close head(3): [121.18, 121.18, 121.18]     # first real close = 121.18
=> back-filled constant before listing: True
```

---

## Trace 11 — Backtest engine cross-check

30 assets × 2 600 business days, monthly rebalance, top-5 momentum, seeded:

| Costs | Backend | Sharpe | Total return | Reported `total_cost` |
| --- | --- | --- | --- | --- |
| zero | vectorbt | 1.2678 | 3.0818 | 0.00000 |
| zero | pandas | 1.2544 | 3.0435 | 0.00000 |
| india/base | vectorbt | 0.9184 | 1.7248 | 0.33385 |
| india/base | pandas | 0.9657 | 1.8952 | 0.33385 |
| india/pessimistic | vectorbt | 0.7589 | 1.2679 | 0.48543 |
| india/pessimistic | pandas | 0.8329 | 1.4869 | 0.48543 |

Two observations:
1. The backends disagree even with **zero** costs (share/cash accounting vs weight-based
   returns) — and `use_vectorbt` defaults to `True` with a **silent** fallback.
2. Costs and turnover are always the pandas values regardless of backend, so
   `metrics.cost_drag` does not reconcile with `metrics.total_return`.

⇒ AUDIT-008.

A look-ahead probe was also run: a daily-rebalance strategy that is long on days when
the asset rose. Under same-bar execution it would be enormously profitable. Measured
Sharpe: **−0.310** for both backends; buy-and-hold −0.762. **No same-bar look-ahead was
found** — `Portfolio.from_orders` fills at bar *t*'s close and accrues from *t+1*, matching the
pandas path's `shift(1)`. This was a hypothesis that the evidence **disproved**, and it is
recorded here so it is not re-investigated.

---

## Trace 12 — Survivorship-bias guard

```
engine.run(prices, weights, strategy_name="s")
  -> ResearchInputError: universe_history is required. Backtests must explicitly
     provide historical index membership to prevent survivorship bias...

engine.run(..., universe_history=[])               -> returns normally
engine.run(..., universe_history=["NOT_A_SYMBOL"]) -> returns normally
pd.testing.assert_series_equal(empty.returns, nonsense.returns)   -> passes
empty.metrics.sharpe == nonsense.metrics.sharpe                   -> passes
```

⇒ AUDIT-007: the parameter is mandatory and completely unused.

---

## Trace 13 — Dashboard UI (every button, every number)

Server started, all routes exercised over HTTP.

| Route | Result | Numbers trace to |
| --- | --- | --- |
| `/healthz` | 200 | stdlib only — **not** a health signal (AUDIT-028) |
| `/api/status` | 200 | `var/operational_status.json` — file does not exist ⇒ every field `"unknown"`, plus `status_error` explaining why. Honest. |
| `/api/overview` | 200 | `datahub.analytics` + paper ledger |
| `/api/operations` | 200 | `dashboard/operations.py` — real heartbeats, real ledger audit. One misleading line (AUDIT-034). |
| `/api/regime` | 200 | `datahub.analytics.regime_summary` over the committed bundle |
| `/api/paper/status` | 200 | SQLite ledger |
| `POST /api/paper/{start,preview,rebalance}` | 200 / error / error | see Trace 4 |

Front-end wiring audit (`dashboard/app/app.js`): 14 distinct `/api/*` paths plus `/guide.md`.
**Every one maps to an existing server route.** No dead buttons, no hardcoded series, no
placeholder numbers were found in `app.js`. The only `localStorage` value is the capital
figure, which is a user preference, not data.

Counter-check on the honesty of the numbers: `/api/overview` reports
`divergence.state = "AWAITING SESSIONS"` with `days_observed: 0` and an explicit reason
("only 1 equity snapshot(s) recorded …") rather than a fabricated tracking error. That is the
correct behaviour and is representative of the better parts of this dashboard.

**The dashboard is, on the whole, honest.** Its problems are inherited from the backend:
it faithfully displays that nothing can be traded (AUDIT-013/035) and one reconciliation
string that suggests an impossible action (AUDIT-034).

---

## Trace 14 — Test-suite forensics

| Observation | Evidence |
| --- | --- |
| Tests mutate committed data | `git status` after a run: `M data/quant.duckdb`, `M data/snapshots/test_snap.parquet` (AUDIT-027) |
| Whole real-data module was dead | Trace 10 |
| A test documented an unimplemented behaviour | `test_adapter_missing_field_raises` expected `DataQualityError("unexpected header")`; no header check existed (AUDIT-005) |
| Secret scan had a false negative | AUDIT-003 |
| Mocked verification of the broker boundary | `execution/service.py` receives the broker duck-typed, so `TestExecutionBoundary` (which forbids importing `broker` from `execution`) is satisfied without the runtime boundary being enforced |
| Architecture tests are real | `tests/test_architecture.py` enforces `risk_kill` stdlib-only, LIMIT-only ordering, no `LIVE` mode, no network in `execution` — all verified by AST/grep and all currently passing |
| No test covers the paper rebalance offline | the reason AUDIT-015 survived |
| No test covers the kill switch gating execution | the reason AUDIT-021 survived |

---

## Trace 15 — What would happen on a fresh machine with a real broker connected

This is the question the brief asks. Answer, from the traces above:

1. **Nothing would place a live order.** There is no code path to a broker order API
   (Trace 8), and that is verified, not assumed.
2. **The paper workflow would not run** — three hard stops before the first virtual order
   (Trace 4).
3. **The orchestrated daily pipeline would halt at step 1** on any real multi-symbol dataset
   (Trace 1.1).
4. **The Docker deployment would crash-loop** (Trace 9).
5. **Every backtest would carry two silent biases** — fabricated pre-listing prices
   (Trace 10) and no survivorship masking at all (Trace 12) — while the completeness report
   claims the affected symbols were excluded.
6. **If any of it did reach execution**, three of the risk checks would be inert (Trace 1.4)
   and the halt path would have crashed (Trace 1.3) — both now fixed.
7. **The FRED key would be compromised** on clone (AUDIT-002).
