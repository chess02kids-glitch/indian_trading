# QUANT INDIA — FULL SYSTEM FORENSIC AUDIT + FUNCTIONAL VERIFICATION

**Audit date:** 2026-09-01
**Repository:** `chess02kids-glitch/indian_trading` (local clone at `/home/user/indian_trading`)
**Base commit:** `80490c3` ("bugs") — single-commit history, 287 non-data Python modules, ~62.8 kLOC
**Auditor role:** principal quant software architect + SRE + security + QA + UI auditor

**Method.** Nothing was assumed to work. Every finding below was reproduced by executing code
in this repository. Documentation was treated as an unverified claim; tests were treated as
claims about behaviour, not as evidence of it; mocks were treated as absences of verification.
No broker was contacted and no order was sent at any point.

**Companion documents**

| Document | Contents |
| --- | --- |
| `CRITICAL_FINDINGS.md` | Every P0/P1 finding in the required template, with evidence |
| `E2E_TRACE.md` | 16 executed end-to-end traces with verbatim output |
| `SYSTEM_STATE_MACHINE.md` | ORDER / POSITION / PORTFOLIO / BROKER / PAPER / KILL-SWITCH / RESEARCH-GATE / AUTH |
| `TEST_GAP_ANALYSIS.md` | Structural and behavioural test gaps; 25 specified missing tests |
| `FIX_PLAN.md` | SAFE TO AUTO-FIX (applied) vs REQUIRES EXTRA CAUTION (proposed patches) |
| `AUDIT_VERIFICATION.md` | How each fix was verified, and what was *not* verified |

---

## 1. Verdict

> ## **NOT FUNCTIONAL**

**Overall score: 4.0 / 10**

I want to be precise about what this verdict does and does not mean, because the number is
low and the engineering is not uniformly bad.

It does **not** mean the code is poor. Several components are genuinely well built: the broker
sandbox is fail-closed at five independent layers, the Indian cost model is complete and
configurable, the walk-forward / CPCV / deflated-Sharpe implementations are correct, the
dashboard never fabricates a green light, and the risk guard genuinely fails closed on unknown
input.

It **does** mean that the system cannot execute its intended workflow. Concretely, on a fresh
clone with correct configuration:

* the orchestrated daily run halts at step 1 on any real multi-symbol dataset
  (137 issues across 20 clean symbols, **zero rows rejected**);
* the paper-trading workflow — the headline feature — hits three independent hard stops before
  a single virtual order can be created;
* the real-data ingestion CLI rejects the argument its own tests and docs use, so the pipeline
  has never run;
* the Docker image cannot start;
* and every backtest carries two silent biases (fabricated pre-listing prices, and no
  survivorship masking at all) while the completeness report claims the affected symbols were
  excluded.

Three of those are now fixed in this change set, and the fourth is fixed. What remains is that
the **research outputs are not trustworthy** and the **protective machinery is not wired to the
paths it protects**. A system that cannot run and whose correct-looking numbers are wrong is
not functional, however good its parts are.

**The honest path to the next verdict.** With the Part B patches B1 (kill switch), B2 (risk
context inputs), B6 (quality-issue classification), B7 (real reconciliation) and B12 (dashboard
auth), the natural verdict becomes **FUNCTIONAL IN PAPER/RESEARCH**. Reaching
**PRODUCTION-CANDIDATE** additionally requires B3 (stop fabricating prices), B4 (implement the
survivorship guard), B8 (NSE calendar + EQ series filtering), a re-baseline of every published
number, and verification against a real broker sandbox. **PRODUCTION-READY** would require, on
top of that, a real broker integration, persistence for the protective state, authentication,
and an operational track record — none of which exists today.

---

## 2. Scorecard

Scored against the brief's rule: **cosmetic quality cannot compensate for safety problems.**
Safety-relevant categories are therefore weighted heavily and a single P0 in a category caps it
near the floor regardless of how polished the rest is.

| # | Category | Score | Reasoning |
| --- | --- | ---: | --- |
| 1 | **Architecture** | **7 / 10** | Clean layering, frozen domain models, explicit protocols, real dependency injection. Docked for two unreconciled risk-limit vocabularies, no portfolio state machine, a 0-byte `execution/state_machine.py`, and `risk_kill/state.json` implying persistence that does not exist. |
| 2 | **Data correctness** | **3 / 10** | **AUDIT-014 (P0)**: pre-listing prices fabricated by back-fill. No NSE holiday calendar (AUDIT-011). Non-EQ series mixed into 63% of symbols (AUDIT-012). No timezone discipline anywhere in `data/quality.py`. No header validation (AUDIT-005). Credit where due: `data/quality.py` is honest about reporting-not-repairing, and the `detect_data_staleness` / `TradingCalendar` design is sound. |
| 3 | **Backtesting correctness** | **4 / 10** | The India cost model is genuinely good — brokerage, STT, exchange, SEBI, stamp duty, GST on (brokerage+exchange+SEBI), scenario spread/slippage, fully env-overridable. But results depend on whether `vectorbt` happens to import (silent fallback), returns and costs come from different simulations (AUDIT-008), and the panel is silently filled (AUDIT-009). |
| 4 | **Research validity** | **3 / 10** | `walk_forward_splits`, `combinatorial_purged_cv` and `deflated_sharpe_ratio` are correct implementations of the published methods — I checked the exclusion radius, the purge/embargo bounds and the Bailey–López de Prado formula. But the survivorship guard is a mandatory-unused parameter (AUDIT-007), the panel is fabricated (AUDIT-014), and the gate leaks three ways (AUDIT-032). Correct tools applied to corrupted inputs. |
| 5 | **Strategy implementation** | **6 / 10** | Verified no look-ahead in the factors, constructors or regime filter: `inverse_volatility` shifts returns by one bar, `regime_series` uses only trailing windows, `momrem_targets` gates then `simulate_weights` applies `exec_lag=1`. I also **disproved** a same-bar look-ahead hypothesis in the VectorBT path (see `E2E_TRACE.md` §Trace 11). Docked for the published-vs-recomputed MomReM discrepancy the README already admits, and for 30 registry strategies with no target builder and no per-strategy validation. |
| 6 | **Risk management** | **3 / 10** | `RiskGuard` is well designed: explicit severity ordering, most-severe-wins, fails closed on `None`. But three of its six checks are structurally dead in the orchestrated path (AUDIT-030), it has no persistence (AUDIT-021), its halt path crashed (AUDIT-001), and two conflicting limit systems exist (AUDIT-024). A beautiful guard that is not connected to the thing it guards. |
| 7 | **Execution safety** | **6 / 10** | LIMIT-only by construction, `OrderType` cannot express MARKET/IOC, idempotency key covers the full order identity, atomic claim, price-band check, rate limiter, token gate before transport, bounded retries with an explicit `UNKNOWN` result. Genuinely strong. Docked for the crashed halt path (now fixed), no order state machine (AUDIT-025), and no kill-switch gate (AUDIT-021). |
| 8 | **Broker integration** | **5 / 10** | As a *safety boundary* this is the best part of the system: five independent fail-closed layers, sandbox-only URL validation, no HTTP client wired in, wire-dialect shaping and fault injection for two brokers. As an *integration* it has never been exercised against a real broker, and the live path is a stub that refuses. Cannot be scored higher without evidence. |
| 9 | **Reconciliation** | **4 / 10** | Position reconciliation is real and independent. Order/fill reconciliation compares the repository against itself and can never fire (AUDIT-022). The engine's own logic (mismatch kinds, lock-on-mismatch, ADR-008) is well written; it is simply fed a tautology. |
| 10 | **Observability** | **6 / 10** | Structured JSON logging with run/symbol/operation context; heartbeats report `"never"` rather than a fake healthy value; `SystemHealth` has monotonic severity. Docked because `/healthz` needs only the standard library and so reports healthy while every panel 503s (AUDIT-028), and because the status document `dashboard/operational.py` reads is written by nothing. |
| 11 | **Security** | **3 / 10** | No live credentials in use, owner-only token files, masked tokens in the API, `tests/test_architecture.py` enforces the real boundaries. But: a live FRED key was committed (AUDIT-002), the scanner that should have caught it was structurally blind (AUDIT-003), the environment validator was never called (AUDIT-020), and **the dashboard has no authentication at all and can disarm the kill switch** (AUDIT-039). |
| 12 | **Database** | **5 / 10** | Migrations with an RLS audit and a `verify_migrations` script; `psycopg2` TLS enforcement; a `with_retries` decorator that correctly does not retry unique-violations. Never exercised against a real Supabase instance; tests use in-memory repositories; tests mutate committed data files (AUDIT-027). |
| 13 | **Deployment** | **2 / 10** | `pip install --no-deps .` ⇒ crash loop (AUDIT-018). `var/` read-only ⇒ kill switch un-armable (AUDIT-019). `data/` read-only while the app writes `data/quant.duckdb` (AUDIT-037). Healthcheck meaningless (AUDIT-028). Wheel omits the `data` package (AUDIT-016). Nothing here works on a fresh machine. |
| 14 | **Dashboard / UI** | **7 / 10** | Genuinely good and honest. All 14 front-end API calls map to real routes; no hardcoded series, no placeholder numbers, no dead buttons were found in `app.js`; missing state renders as an explicit `"unknown"` with a reason; the divergence panel correctly says `AWAITING SESSIONS` with `days_observed: 0` instead of inventing a tracking error. Docked for one misleading reconciliation string (AUDIT-034), the `source: "UPSTOX"` label on SIM-priced quotes, and no authentication (AUDIT-039). |
| 15 | **Testing** | **5 / 10** | 1264 tests; the architecture tests are real (stdlib-only `risk_kill`, LIMIT-only, no LIVE, no network in `execution`). But the only real-data module was dead (9 tests never ran), a security test had a false negative, there are no UI tests, no deployment tests, and tests mutate committed data. **After this change set: 6/10** — the dead module runs and 26 regression tests guard the findings. |
| 16 | **Production readiness** | **2 / 10** | Cannot be deployed (Docker crash-loops), cannot complete its workflow, research outputs are invalidated, kill switch is bypassable and un-armable in the container, no authentication. |
| | **OVERALL** | **4.0 / 10** | Safety-weighted. Categories 2, 4, 6, 11, 13 and 16 each contain a P0 or a "cannot run at all" defect, and per the brief's rule the polished categories (1, 7, 14) cannot compensate for them. |

---

## 3. Dependency map (reconstructed, as actually wired)

```
 ┌─ DATA SOURCES ──────────────────────────────────────────────────────────────────┐
 │  data/eod2/daily/*.csv (3 694 syms, eod2_data mirror, split/bonus adjusted)      │
 │  data/membership/index_history/…csv (44 NSE indices, PIT membership)             │
 │  Upstox read-only quotes (UPSTOX_ACCESS_TOKEN) → SIM → EOD  [datahub.quotes]     │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ INGESTION ─────────────────────────────────────────────────────────────────────┐
 │  ingestion/eod2_adapter         (header validated AFTER this audit)              │
 │  ingestion/nse_membership_adapter  → data/universe/<slug>-pit/  (PIT)            │
 │  scripts/ingest_real_data.py    (BROKEN at HEAD — AUDIT-004)                     │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ VALIDATION ────────────────────────────────────────────────────────────────────┐
 │  data/quality.py: check_ohlcv_long_frame, validate_market_bars (as_of NOW wired),│
 │  detect_data_staleness (6 d), detect_missing_candles, detect_off_calendar,       │
 │  TradingCalendar (WEEKDAYS ONLY — AUDIT-011)                                     │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ STORAGE ───────────────────────────────────────────────────────────────────────┐
 │  raw: data/raw/eod2_data/NSE/<SYM>/<YYYY>/<MM>.parquet (5 686 files)             │
 │  clean: data/clean/eod2_data/<SYM>.parquet (133 syms, 1995 → 2026-08)            │
 │  DuckDB: data/quant.duckdb  ·  Supabase (prod) · SQLite (paper ledger)           │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ FEATURES / SIGNALS ────────────────────────────────────────────────────────────┐
 │  research/factors.py (momentum, RSI, ATR, z-score, Bollinger, MA cross)          │
 │  models/quality.py  (ROE, P/E, debt, composite)                                  │
 │  research/strategies.py 30 strategies; datahub/analytics.momrem_targets          │
 │  ── verified trailing-only, no look-ahead ──                                     │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ ENSEMBLE / CONSTRUCTION ───────────────────────────────────────────────────────┐
 │  portfolio/construction.py: equal_weight, inverse_volatility, risk_parity        │
 │  long-only projection with min/max weight bounds                                 │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ BACKTEST ──────────────────────────────────────────────────────────────────────┐
 │  backtest/engine.py (vectorbt | pandas, SILENT fallback)                         │
 │  backtest/costs.IndiaCostModel ← config/costs.IndiaChargeTable                   │
 │  backtest/validation.py (walk-forward, CPCV, DSR, bootstrap)                     │
 │  ⚠ universe_history REQUIRED BUT IGNORED (AUDIT-007)                             │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ RESEARCH GATE ─────────────────────────────────────────────────────────────────┐
 │  research/gate.py → PASS | FAIL | FRAGILE | INSUFFICIENT_EVIDENCE                │
 │  ⚠ three leaks (AUDIT-032)                                                       │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ RISK ──────────────────────────────────────────────────────────────────────────┐
 │  risk_kill/guard.py (stdlib-only, 6 states, fails closed)                        │
 │  ⚠ NO PERSISTENCE · 3 CHECKS DEAD IN PIPELINE (AUDIT-030) · state.json is dead  │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ ORCHESTRATION ─────────────────────────────────────────────────────────────────┐
 │  orchestration/pipeline.py: claim_run → validate (HALTS ON ANY ISSUE) → signals  │
 │    → constructor → risk → approval gate → execution → reconciliation → health    │
 │  ⚠ does NOT consult the operator kill switch (AUDIT-021)                         │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ EXECUTION ─────────────────────────────────────────────────────────────────────┐
 │  execution/service.py → validation (LIMIT-only) → duplicate check → idempotency   │
 │    → broker → positions                                                          │
 │  broker/safe_execution.py → mode gate, price band, rate limit, token gate, retry │
 │  broker/transport.py → SimulatedSandboxTransport ONLY. NO LIVE PATH. ✅           │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ RECONCILIATION ────────────────────────────────────────────────────────────────┐
 │  reconciliation/engine.py: positions (INDEPENDENT ✅) + orders/fills (SELF ⚠)    │
 └───────────────────────────────┬─────────────────────────────────────────────────┘
                                 ▼
 ┌─ OBSERVABILITY / DASHBOARD / ALERTS ────────────────────────────────────────────┐
 │  observability/health.py, datahub/state.py (heartbeats + kill switch)            │
 │  dashboard/server.py (stdlib HTTP, 0.0.0.0, NO AUTH ⚠ AUDIT-039)                 │
 │  dashboard/app/app.js (14 API calls, all wired ✅)                                │
 └─────────────────────────────────────────────────────────────────────────────────┘

 SEPARATE, PARALLEL PATH (the product's headline workflow):
   paper_trading/service.py → Upstox read-only quotes (QuoteChain: UPSTOX→SIM→EOD)
                            → preview_rebalance → risk → execute_virtual_fill → SQLite
   ⚠ BLOCKED three ways out of the box: not paper-approved / stale signal / no token
```

---

## 4. What actually works — verified

I want to give credit where it is due, because a list of defects alone would misrepresent this
codebase.

1. **There is no path to a live broker order.** Verified, not assumed: `grep` over every
   network-capable primitive in `broker/`, `paper_trading/`, `dashboard/live/`, `datahub/` and
   `dashboard/` finds only `urllib.parse.unquote`. `broker/transport.py`'s HTTP stub validates
   that its URL is a sandbox endpoint and then **refuses to send**. `validate_sandbox_base_url`
   rejects anything that is not `simulated://`, loopback, or `sandbox.*`.
   `BaseSandboxAdapter.__init__` raises `LiveTradingDisabledError` on `LIVE`.
   `models/domain.py::ExecutionMode` has no `LIVE` member; `OrderType` has only `LIMIT`.
   **This is the most valuable property the system has, and it is real.**

2. **The Indian cost model is complete and honest.** Brokerage, STT (buy and sell separately),
   exchange transaction charges, SEBI charges, stamp duty (buy only), GST at 18% on
   (brokerage + exchange + SEBI), plus scenario spread and slippage — all configurable through
   `QUANT_COST_*` environment variables, with a `TABLE_VERSION` string that literally reads
   `"india-charges-2026.08 (verify before production)"`. It does not pretend to be permanent
   truth.

3. **The validation methods are correct.** `combinatorial_purged_cv` applies purge and embargo
   symmetrically around every test group and raises if training is emptied.
   `deflated_sharpe_ratio` implements Bailey & López de Prado (2014) correctly, including the
   Euler–Mascheroni weighting in `expected_maximum_sharpe` and the skewness/kurtosis variance
   correction. `walk_forward_splits` enforces purge < train_size and non-overlapping tests.

4. **No look-ahead in the signal stack.** Verified by reading and by probing:
   `inverse_volatility` shifts returns one bar before computing volatility; `regime_series`
   uses only trailing windows (Kaufman efficiency, 756-day rolling vol rank, 100-day SMA);
   `momrem_targets` gates weights at day *t* and `simulate_weights` applies `exec_lag=1`.
   I explicitly tested the VectorBT path for same-bar fills and **disproved** it — an "oracle"
   strategy that is long on days the asset rose returned Sharpe −0.310, not infinity.

5. **`RiskGuard` fails closed on unknown input.** `broker_connected=None → LOCK_ACCOUNT`,
   `data_last_updated=None → LOCK_ACCOUNT`, `equity_day_start=None → LOCK_ACCOUNT`. That is the
   right instinct and it is implemented consistently.

6. **The dashboard does not fake numbers.** `/api/status` returns `"unknown"` for every field
   when the status file is absent, plus a `status_error` explaining why. The divergence panel
   reports `AWAITING SESSIONS` with `days_observed: 0` instead of inventing a tracking error.
   All 14 front-end API calls map to real server routes; no hardcoded series or placeholder
   values were found in `app.js`.

7. **Heartbeats report `"never"`, not green.** `datahub/state.py` returns
   `{"at": None, "state": "never"}` for a heartbeat that has never fired, and the Operations
   page renders that as an explicit warning.

8. **The kill switch, where it is consulted, is consulted first.** In
   `paper_trading/service.py`, `is_killed()` is the very first check in both
   `execute_rebalance` and `run_automation_once`, and automation additionally requires the
   literal confirmation string `"ENABLE AUTO PAPER"` **and** a `paper_approved` strategy
   **and** IST market hours (09:15–15:30).

---

## 5. What fails, and what silently behaves incorrectly

### 5.1 Fails outright

| What | Evidence |
| --- | --- |
| Orchestrated daily run on real data | 137 issues / 20 symbols / 0 rows rejected ⇒ `halted_data_quality` |
| Paper rebalance | 3 sequential hard stops (AUDIT-013 → 035 → 015) |
| `scripts/ingest_real_data.py --local --universe-dir …` | `argparse` exit 2 |
| Completeness report | `KeyError: 'rows'` |
| Docker image | `pip install --no-deps .` ⇒ `ImportError: numpy` before the port binds |
| `pytest tests/` on a clean install | `ModuleNotFoundError: seaborn` at collection |
| CI at HEAD | ruff check (10 errors), ruff format (2 files), pytest (2 failed, 7 errors) |

### 5.2 Silently behaves incorrectly — the dangerous category

These are worse than outright failures, because a failure is visible and a silent wrong answer
is not.

1. **Fabricated price history (AUDIT-014).** `NEWCO` carries a constant 121.18 for 306 sessions
   before it listed, and the completeness report lists it under `excluded_symbols`.
2. **A mandatory parameter that does nothing (AUDIT-007).** `universe_history` is required with
   a stern error message and then never read. `universe_history=[]` and
   `universe_history=["NOT_A_SYMBOL"]` produce bit-identical results.
3. **Risk checks that cannot fire (AUDIT-030).** `equity_day_start == equity_now` is an
   arithmetic identity; `broker_connected=True` and `order_timestamps=()` are constants. Three
   of six checks are dead and the system still reports `NOMINAL`.
4. **Tautological reconciliation (AUDIT-022).** "Expected" and "actual" orders come from the
   same repository, so `matched: True` for fills is a certainty, not a finding.
5. **A security test that passes while its subject leaks (AUDIT-003).** `\b` cannot match after
   `_`.
6. **Backend-dependent results (AUDIT-008).** `use_vectorbt=True` with a *silent* fallback
   means the same code produces different Sharpe on different machines, and the reported
   `total_cost` always comes from the other backend.
7. **A misleading quote label.** `broker_health.last_quote_detail.source` reads `"UPSTOX"` (the
   configured mode) while the chain actually served SIM.
8. **A kill-switch file that no code reads (AUDIT-021).** `risk_kill/state.json` says
   `"status": "ARMED"`; nothing reads it and `RiskGuard` persists nothing.

### 5.3 What could create an unsafe trade

Given that no live order path exists, the honest answer is: **today, nothing in this repository
can place a real order.** That is the single most important safety fact and it is verified.

But the controls that would stop an unsafe trade once a live path *is* added are exactly the
ones that are broken:

* the kill switch does not gate execution or orchestration (AUDIT-021);
* it can be **disarmed by an unauthenticated HTTP request** (AUDIT-039);
* three of six risk checks are structurally dead (AUDIT-030);
* the protective halt path raised `AttributeError` instead of halting (AUDIT-001);
* reconciliation cannot detect a missing or duplicate fill (AUDIT-022);
* the order state machine does not exist (AUDIT-025), so a `FILLED → PENDING` regression would
  not be rejected;
* and the signals that would drive those trades are computed from a panel containing fabricated
  prices (AUDIT-014) with no survivorship masking (AUDIT-007).

In other words: the *absence* of live trading is currently doing the work that the risk layer
is supposed to do. That is the finding that matters most.

---

## 6. Remaining findings (P2 / P3), expanded

(Full P0/P1 write-ups are in `CRITICAL_FINDINGS.md`.)

**AUDIT-024 (P2) — Two conflicting risk-limit systems.**
`risk_kill.RiskLimits`: max position 25%, gross 100%, daily loss 3%, drawdown 10%, data age
18 h. `paper_trading.DEFAULT_RISK_POLICY`: max position 15%, gross 100%, daily loss 3%,
drawdown **15%**. Nothing defines which governs, and the paper path never consults `risk_kill`
at all — so the drawdown limit that applies depends on which code path you are in.

**AUDIT-025 (P2) — `execution/state_machine.py` is a 0-byte file.**
The documented order state machine does not exist. `PaperBroker` mutates a plain
`dict[str, OrderResult]` in `cancel_order` and `_expire_stale_orders` with no transition
validation, so `FILLED → PENDING`, `REJECTED → FILLED` and re-filling a cancelled order are all
representable. Nothing currently performs them — but nothing would stop a future fill handler.

**AUDIT-026 (P3) — CLI argument parsing.**
`main.py`: `default=int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080`
— index arithmetic on `sys.argv` inside a default, which raises `IndexError` for a trailing
`--port`.

**AUDIT-027 (P2) — Tests mutate committed data.**
`git status` after a plain `pytest tests/`:
```
 M data/quant.duckdb
 M data/snapshots/test_snap.parquet
```
Test runs are not reproducible against a clean checkout.

**AUDIT-028 (P2) — `/healthz` is not a health check.**
It needs only the standard library, so the Docker/compose healthcheck reports healthy while
`get_paper_service()` raises and every `/api/*` panel fails. Verified by the import-blocking
experiment.

**AUDIT-029 (P2) — Three staleness thresholds, an 8× spread.**
6 days in `data.quality.validate_market_bars`, 18 hours in `risk_kill.RiskLimits`, 5 days in
`dashboard.strategy_dashboard`. The data layer tolerates data the risk layer refuses to trade
on.

**AUDIT-031 (P3) — Committed sample does not match its model.**
`execution/orders.jsonl` uses `price`, `expected_price`, `actual_price`; `OrderResult` defines
`limit_price` and `average_fill_price`.

**AUDIT-033 (P2) — Streamlit is not a declared dependency.**
`dashboard/main_dashboard.py`, `paper_dashboard.py`, `research_dashboard.py` and
`broker_dashboard.py` import `streamlit`, which appears in neither `dependencies` nor `dev`.
`make dashboard` fails on a clean install. (`dashboard/server.py`, the one that is actually
served, is stdlib-only — good.)

**AUDIT-034 (P2) — A UI instruction the system will refuse.**
`/api/operations` says *"the signal expects 20 positions but the paper account is flat — run a
paper rebalance (or enable auto-paper) to track it"*, while a rebalance is impossible
(AUDIT-013) and the signal is stale (AUDIT-035).

**AUDIT-036 (P2) — The documented setup is rejected by the project's own validator.**
`.env.example` documents `UPSTOX_API_KEY` and `UPSTOX_API_SECRET`;
`config/env_validator.validate_environment` **refuses to start** if either is set. The example
also sets `SYSTEM_MODE=LOCAL` while the validator defaults to `PAPER`, which requires
`DATABASE_URL` — so a clean checkout with no `.env` fails its own preflight:
```
$ python scripts/preflight.py
[FAIL] environment: ConfigurationError: DATABASE_URL must be set in PAPER mode.
```

**AUDIT-037 (P2) — `data/` is mounted read-only while the app writes to it.**
`docker-compose.yml` mounts `./data:/app/data:ro`, but the application writes
`data/quant.duckdb` (AUDIT-027). Ingestion inside the container would fail.

**AUDIT-038 (P3) — Only one caller sets `tested_variants`.**
`research/candidate_set.py:514` is the only site; every other `gate.evaluate` call gets
`trials = len(benchmarks) + len(placebos) + 1`, which is the low end of AUDIT-032.

---

## 7. Indian-market domain: verified vs assumed vs documented

| Requirement | Status | Evidence |
| --- | --- | --- |
| NSE/BSE symbol conventions (`.NS`/`.BO`) | **Assumed, unused** | The ingestion path normalises to bare NSE symbols (`RELIANCE`, `M&M`) and sets `exchange="NSE"` explicitly. No `.NS`/`.BO` suffix logic exists anywhere; the eod2 mirror is the only source. |
| Instrument/symbol → broker token mapping | **Documented, unverified** | `paper_trading/instruments.py::load_nifty_instruments` maps Nifty 500 symbols to Upstox instrument keys. The mapping file is committed; correctness against Upstox was not verified (no token). |
| Trading sessions (09:15–15:30 IST) | **Verified** | `datahub/quotes.py`: `SESSION_OPEN_MIN = 555`, `SESSION_CLOSE_MIN = 930`; `paper_trading` automation requires `555 ≤ minute ≤ 930` on an IST weekday. Correct. |
| NSE holidays | **MISSING** | `nse_weekday_calendar()` is weekday-only. `is_trading_day` returns `True` on Diwali. (AUDIT-011) |
| Special sessions | **Treated as errors** | 50 Saturday + 11 Sunday candles in 20 symbols; 2025-02-01 and 2026-02-01 are real Union Budget sessions flagged as `off_calendar`. (AUDIT-011) |
| IST/UTC handling | **Partial** | `datahub/quotes.py`, `paper_trading/service.py` and (after this audit) `orchestration/pipeline.py` use `ZoneInfo("Asia/Kolkata")`. `data/quality.py` uses naive timestamps throughout. Mixed discipline. |
| Corporate actions | **Documented, partially applied** | `Eod2SourceSpec.adjustment_state = "split_bonus_adjusted"` with an explicit note that dividends are **not** adjusted. Correctly recorded in provenance — a real strength. |
| Equity series (EQ/BE/SM/ST/BZ) | **Not filtered** | ~8% of rows across 63% of symbols. (AUDIT-012) |
| SEBI/broker charges | **Verified complete** | brokerage, STT (buy+sell), exchange, SEBI, stamp duty, GST on the right base. (§4.2) |
| Delivery (CNC) vs intraday | **Delivery assumed** | The charge table is labelled "delivery (CNC) cash equity"; there is no intraday/MIS path or charge variant. |
| Current regulatory rates | **NOT VERIFIED** | I did not verify the charge table against current authoritative sources this cycle. `TABLE_VERSION` itself says `"verify before production"`. **This is an open item, not a clean bill of health.** |

---

## 8. Cross-module contract verification

| Contract | Verified? | Notes |
| --- | --- | --- |
| `MarketData` panels align (index + columns) | ✅ | `research/contracts.py::_validate_panel` and `MarketData.__post_init__` enforce it |
| `Signal` aligns with `MarketData` | ✅ | enforced by the constructors |
| `PortfolioTarget` → `OrderIntent` | ✅ | frozen models, LIMIT-only with a positive finite price validator |
| `OrderResult` vs `BrokerOrderRecord` | ⚠ | `SandboxExecutionAdapter._to_result` maps them; but committed `execution/orders.jsonl` uses a different key vocabulary (AUDIT-031) |
| `RiskState` → `SystemHealth` | ❌ → ✅ | was `AttributeError`; now `risk_kill/mapping.py` (AUDIT-001) |
| `QuoteResult` vs `MarketQuote` | ⚠ | two quote types with the same field names; `preview_rebalance` used `MarketQuote`-shaped access on `QuoteResult` objects (`quote.timestamp` vs `source_timestamp`) until this audit (AUDIT-015) |
| Membership audit → completeness report | ❌ → ✅ | `KeyError: 'rows'`; now `merge_membership_audit()` (AUDIT-004) |
| `UniverseDataset.from_dir` layout vs `ingest_membership` writer | ❌ → ✅ | root vs `<slug>-pit` (AUDIT-004) |
| Repository protocols (`store/protocols.py`) vs `store/memory.py` | ✅ | in-memory implementations satisfy the protocols |
| Front-end API calls vs server routes | ✅ | all 14 map |
| `config/paper_strategies.json` vs `DEFAULT_REGISTRY` | ❌ | `momrem` missing from the file (AUDIT-013) |

---

## 9. Dead code, TODOs and misleading artefacts

| Item | Status |
| --- | --- |
| `execution/state_machine.py` | **0 bytes** (AUDIT-025) |
| `risk_kill/state.json` | read by nothing; implies persistence that does not exist (AUDIT-021) |
| `dashboard/operational.py`'s status document (`var/operational_status.json`) | written by nothing; the module correctly reports `"unknown"`, and its own docstring notes the previous page "printed a table of unknown values read from a status JSON file that nothing in the repository ever wrote" |
| `config/env_validator.validate_environment` | no caller outside tests (AUDIT-020) |
| `HttpSandboxTransportStub` | refuses to perform requests by design — documented as the seam for a future client |
| `dashboard/main_dashboard.py:598` | `# In a real implementation, this would write to risk_kill/state` — an unfinished write path, and the only writer of that dead file |
| `main.py validate` command | prints `"Full DB validation coming soon."` |
| `research.realdata.build_market_panels` | `# We no longer exclude, but just note it` — the only record of an undocumented behaviour change (AUDIT-014) |
| `execution/service.py` `# nosec B104` on the dashboard bind | suppresses the binding warning without adding authentication (AUDIT-039) |

No `TODO`/`FIXME` blocks were found that describe an unfinished safety control other than those
listed above.

---

## 10. Changes made in this audit

**Applied (SAFE TO AUTO-FIX)** — 14 fixes across 15 files, plus 26 new regression tests.
Full detail and per-fix verification in `FIX_PLAN.md` Part A and `AUDIT_VERIFICATION.md`.

| Metric | Before | After |
| --- | --- | --- |
| `pytest tests/` | 1250 passed, **2 failed, 7 errors** | **1285 passed, 0 failed, 0 errors**, 5 skipped, 3 xfailed |
| Tests collected | 1264 | 1293 (the count moves with the committed DuckDB — AUDIT-027) |
| `ruff check` (CI paths) | **10 errors** | All checks passed |
| `ruff format --check` | **2 files would be reformatted** | 50 files already formatted |
| Real-data test module | 9 tests dead | 21 passed, 3 xfailed (AUDIT-014) |

**Not applied (REQUIRES EXTRA CAUTION)** — 12 items in `FIX_PLAN.md` Part B, each with the
precise patch and the behavioural change it would cause. None of them is cosmetic; all of them
move either a safety policy or a published number.

**Immediate owner action required, outside the code:** rotate the FRED API key (AUDIT-002). It
is in the pushed git history; removing it from the working tree does not un-publish it.

---

## 11. The honest answer

> *"If I cloned this repository onto a completely fresh machine today, configured it correctly,
> connected a real Indian broker account, and ran the intended workflow — what would actually
> work, what would fail, what would silently behave incorrectly, and what could potentially
> create an unsafe trade?"*

**What would work.**

The data platform. Ingestion of the committed eod2 mirror into the raw and clean Parquet layers
works. The point-in-time Nifty 50/100/500 universe builds correctly, including delisted-name
retention and demerger-dummy exclusion — I verified this end-to-end on the repository's own
fixture world. Feature computation, the 30 strategies, the portfolio constructors and the
research validation methods (walk-forward, CPCV, deflated Sharpe, bootstrap CIs) are correctly
implemented. The Indian cost model is complete and configurable. The dashboard starts, every
button is wired to a real endpoint, every number traces to a backend value, and it reports
`unknown` rather than inventing a green light.

**What would fail.**

The orchestrated daily run would halt at data validation on the first real dataset — 137 issues
across 20 perfectly ordinary symbols, with not one row actually rejected. The paper-trading
workflow would stop three times before creating a single virtual order: no strategy is
paper-approved, the shipped signal is 7 days stale, and the rebalance path demands an Upstox
access token even though the rest of the app happily prices from a simulator. The real-data
ingestion CLI would reject the argument its own documentation uses. The completeness report —
the artefact that pipeline exists to produce — would crash with `KeyError: 'rows'`. If you
tried the Docker path, the container would crash-loop before binding its port, and if you fixed
that you still could not arm the kill switch because `var/` is mounted read-only. And on a
clean install, `pytest tests/` would not even collect: `ModuleNotFoundError: No module named
'seaborn'`.

**What would silently behave incorrectly.**

The research numbers. The panel contains prices for stocks that did not exist yet — I measured
one: a constant 121.18 carried back over 306 sessions before listing — and the completeness
report lists that symbol under `excluded_symbols` anyway. The survivorship guard is a parameter
you are *required* to pass and that is then never read; passing `[]` or `["NONSENSE"]` gives
bit-identical results. Three of the six risk checks are arithmetic impossibilities: the
daily-loss check subtracts a number from itself, the broker-connectivity check reads a hardcoded
`True`, and the order-rate check is handed an empty tuple. Reconciliation compares the order
store against itself for fills, so `matched: True` is a certainty rather than a finding. And the
backtest quietly uses a different execution model depending on whether `vectorbt` happens to
import on your machine, while always reporting the *other* model's costs.

**What could create an unsafe trade.**

Today, nothing — and I want to be unambiguous about that, because I verified it rather than
took it on trust. There is no code path from this repository to a broker order API. The broker
package refuses `LIVE` at construction, refuses non-sandbox URLs, refuses to construct an HTTP
client, and `models.domain` cannot even represent a MARKET order. That is the system's most
valuable property and it is real.

But that safety comes from an *absence*, not from the controls. Every control that would stop
an unsafe trade once a live path exists is broken: the kill switch is not consulted by
execution or orchestration, it can be disarmed by an unauthenticated POST to the dashboard
(which listens on `0.0.0.0` with no authentication of any kind), three risk checks cannot fire,
the protective halt path raised `AttributeError` instead of halting, reconciliation cannot
detect a duplicate or missing fill, there is no order state machine to reject an illegal
transition, and the deterministic guard persists nothing so a restart forgets that the account
was locked.

So the honest summary is this: **the repository is safe because it cannot trade, not because it
trades safely.** The engineering underneath is better than the verdict suggests — the sandbox
boundary, the cost model, the validation methods and the dashboard's honesty are all genuinely
good work, better than I expected. What is missing is the wiring: between the panel and the
truth, between the guard and the paths it guards, and between the kill switch and the orders it
is supposed to stop.

Fix the wiring — B1, B2, B6, B7, B12 in `FIX_PLAN.md` — and this becomes a credible
paper/research system. Fix the data — B3, B4, B8 — and the research numbers start meaning
something. Until then, treat every backtest this repository produces as indicative, not as
evidence, and do not connect a broker.
