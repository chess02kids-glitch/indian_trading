# SYSTEM STATE MACHINE AUDIT

Audit date: 2026-09-01 · Base commit `80490c3`

The brief asks for an audit of the ORDER / POSITION / PORTFOLIO / BROKER / PAPER /
KILL-SWITCH / RESEARCH-GATE / AUTH state machines. This document records, for each,
**where the state actually lives, what transitions are enforced, and which transitions are
merely representable**. "Representable" means the data structure allows a state that no code
produces or validates — i.e. an illegal state that nothing would stop.

---

## Summary

| Machine | States defined in | Transitions enforced by | Illegal states representable | Verdict |
| --- | --- | --- | --- | --- |
| ORDER | `models/domain.py::OrderStatus` | `PaperBroker`, `SimulatedBrokerBackend` — by dict mutation | **Yes** | Weak |
| POSITION | `models/domain.py::Position` (frozen) | derived from fills; no machine | n/a | Adequate |
| PORTFOLIO | none | none | n/a | **Absent** |
| BROKER (session/auth) | `broker/mode.py::OperatingMode`, `broker/token.py` | constructor + `check_execution_permitted` | No | Strong |
| PAPER | `paper_trading/ledger.py` (SQLite settings) | explicit guards + confirmation strings | Minor | Good |
| KILL-SWITCH | `risk_kill/guard.py::RiskState`, `datahub/state.py` | `RiskGuard.evaluate` (total order); `datahub.state` (flag) | n/a | **Split / incomplete** |
| RESEARCH-GATE | `research/gate.py::GateVerdict` | `ResearchGate.evaluate` | No (enum-validated) | Adequate (policy too loose) |
| AUTH | `broker/token.py::TokenRecord` | `TokenManager.get_token` + `StaleTokenError` | No | Strong |

---

## 1. ORDER

**States** (`models/domain.py`):
`PENDING → PARTIALLY_FILLED → FILLED`, with `REJECTED`, `CANCELLED`, `EXPIRED` as terminal
side-states. `OrderStatus` is a `str`-valued enum and `OrderResult` is a frozen pydantic model.

**Where transitions happen:**

| Implementation | Location | Mechanism |
| --- | --- | --- |
| `execution/paper.py::PaperBroker` | `_orders: dict[str, OrderResult]` | direct assignment: `self._orders[key] = filled` |
| `broker/simulated.py::SimulatedBrokerBackend` | internal order table | deterministic fills driven by `BEHAVIOURAL_FAULTS` |
| `broker/adapter.py::BaseSandboxAdapter` | maps canonical → wire status | `_UPSTOX_STATUS` / `_DHAN_STATUS` tables |

**Finding AUDIT-025:** `execution/state_machine.py` is a **0-byte file**. The architecture
documents an order state machine; the file that should contain it is empty.

**Consequence — illegal transitions are representable.** There is no transition table and no
validation function. `PaperBroker.cancel_order` and `_expire_stale_orders` overwrite
`self._orders[key]` unconditionally, so:

* `FILLED → PENDING` is representable (overwrite a filled result with a pending one);
* `REJECTED → FILLED` is representable;
* a `CANCELLED` order can be re-filled by a late fill message.

Nothing in the current code path performs those transitions, so this is a latent defect
rather than an active one — but there is no guard, and a future fill handler would not be
stopped.

**Duplicate suppression** is the one part that is genuinely strong:
`execution/idempotency.py::compute_idempotency_key` hashes
`strategy / hypothesis / symbol / side / quantity / limit_price / order_type / rebalance_date`;
`IdempotencyRegistry.claim` is atomic; `SandboxExecutionAdapter` returns the stored result for a
repeat key without contacting the broker; `reconciliation/engine.py::_check_duplicates` flags
two orders sharing a key.

**But** see AUDIT-022: in `orchestration/pipeline.py` the "expected" and "actual" order lists
both read `self.order_repository`, so `_check_duplicates` and `_check_fills` compare the store
to itself and can never fire.

**Recommended:** one `ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]]` table and a
single `apply_transition(current, next)` used by every writer. Patch in `FIX_PLAN.md`.

---

## 2. POSITION

`Position` is a frozen pydantic model (`symbol`, `quantity`, `average_price`, …). There is no
position state machine; positions are *derived* from fills.

Two derivations exist and they are independent:

* `reconciliation/engine.py::_check_positions` compares expected quantities against
  `broker.get_positions()` — the only genuinely independent reconciliation in the system.
* `paper_trading/ledger.py` maintains positions in SQLite from `execute_virtual_fill`.

`orchestration/pipeline.py::_expected_state` derives expected quantities by replaying stored
fills with a `+1 / -1` direction per side. A fill the system did not record cannot appear in
`expected`, but it *can* appear in `broker.get_positions()` — and it is flagged as
`unexpected_position`. That check works.

Verdict: adequate. Positions are the only part of reconciliation that is not tautological.

---

## 3. PORTFOLIO

**There is no portfolio state machine.** There is no `PortfolioState`, no lifecycle object, and
no component that owns "the portfolio" as a stateful entity.

What exists instead:

* `research.contracts.MarketData` + `Signal` + constructors → **target weights** (research).
* `models/domain.py::PortfolioTarget` — a **frozen, immutable** desired end-state
  (`strategy_id`, `hypothesis_id`, `as_of`, `limits`, `target_quantities`).
* `orchestration/pipeline.py::_build_target` builds one from signals each day.

So the "portfolio" is recomputed from scratch on every run from signals plus the broker's
current positions. There is no state to corrupt, but there is also **no state to audit**: the
system cannot answer "what did the portfolio look like yesterday, according to us" from a
portfolio object — only by replaying the order ledger.

**Consequence:** the daily-loss and drawdown checks have no historical portfolio state to draw
on, which is the root cause of AUDIT-030 (`equity_day_start = equity_now`).

**Recommended:** persist an `equity_snapshots(date, equity, cash, market_value)` table written by
every mark-to-market, and source `equity_day_start` / `equity_peak` from it.

---

## 4. BROKER (session / authorisation)

**States:** `broker/mode.py::OperatingMode` — `RESEARCH`, `PAPER`, `SANDBOX`, `LIVE`.

**Enforcement — this is the strongest machine in the repository:**

| Transition | Guard | Location |
| --- | --- | --- |
| construct adapter in `LIVE` | `raise LiveTradingDisabledError` | `broker/adapter.py` |
| construct adapter outside `SANDBOX` | `raise SandboxOnlyError` | `broker/adapter.py` |
| wrap a non-sandbox adapter | `check_execution_permitted(adapter.mode)` | `broker/safe_execution.py` |
| non-sandbox base URL | `validate_sandbox_base_url` → `LiveTradingDisabledError` (only `simulated://`, loopback, `sandbox.*`) | `broker/transport.py` |
| HTTP sandbox client | validates the URL, **then refuses to perform the request** | `broker/transport.py::HttpSandboxTransportStub` |
| stale/absent token | `StaleTokenError` → deterministic `REJECTED` order result **before** any transport call | `broker/safe_execution.py` |
| expiry countdown | `TokenRecord.seconds_until_expiry`, `is_expired`, `masked_token` | `broker/token.py` |

Verified end-to-end: `grep -rn "requests|httpx|urllib|socket|urlopen"` across `broker/`,
`paper_trading/`, `dashboard/live/`, `datahub/` and `dashboard/` finds only
`urllib.parse.unquote` in the HTTP server. **There is no code path to a broker order API.**

Illegal states are **not** representable: `LIVE` construction raises, and the transport has no
client. This machine fails closed at every layer.

**One gap:** `broker.mode.OperatingMode.LIVE` exists as an enum member even though it is
unreachable. It is a tripwire (fine), but `models/domain.py::ExecutionMode` has no `LIVE`
member at all — so the two mode vocabularies disagree about whether live exists. Cosmetic, P4.

---

## 5. PAPER

**State lives in** `paper_trading/ledger.py` — a SQLite `settings` row
(`running`, `paused_at`, `started_at`, `data_mode`, `cash`, `watchlist`, `risk_policy`,
`auto_paper_enabled`, `auto_strategy`, `last_auto_rebalance_at`, `benchmark_start_price`, …)
plus `positions`, `orders`, `fills`, `equity_history`, `events`, `marks` tables.

**Transitions and guards — good:**

| Transition | Guard |
| --- | --- |
| `stopped → running` | `start_monitor()` |
| `running → paused` | `pause()` |
| `* → reset` | `reset()` requires the literal string `"RESET PAPER"` |
| `auto_paper off → on` | requires `"ENABLE AUTO PAPER"` **and** a `paper_approved` strategy |
| `preview → execute` | requires `"PAPER REBALANCE"`, `running == True`, `preview["ready"]`, and `is_killed() == False` |
| any rebalance | kill switch checked **first** (`paper_trading/service.py:657`) |
| automation tick | IST weekday check **and** 555 ≤ minute-of-day ≤ 930 (09:15–15:30 IST) |

Kill-switch precedence is correct: it is the first check in both `execute_rebalance` and
`run_automation_once`.

**Weaknesses:**

1. Only `momrem` has a target builder; every other strategy raises
   `"no paper target builder has been registered for this strategy"` (AUDIT-013).
2. `preview_rebalance` bypassed the quote chain (AUDIT-015, now fixed).
3. Fills are immediate and complete at the observed bid/ask — there is no partial-fill or
   rejection state in the paper path. That is a fidelity gap (a real LIMIT order can sit
   unfilled), not a safety defect, but it means the paper account systematically over-states
   fill certainty.
4. `set_risk_policy` validates ranges but does not reconcile with `risk_kill.RiskLimits`
   (AUDIT-024).

---

## 6. KILL-SWITCH

**Two independent, unreconciled machines.** This is the most serious state-machine defect in
the repository.

### 6a. Deterministic guard — `risk_kill/guard.py`

`RiskState` with an explicit severity order (`_SEVERITY`):

```
NOMINAL (0) < ALERT_HUMAN (1) < STOP_NEW_ORDERS (2) < CANCEL_OPEN_ORDERS (3)
            < FLATTEN_POSITIONS (4) < LOCK_ACCOUNT (5)
```

`RiskGuard.evaluate` runs every check and returns the **most severe**. The checks fail closed
on unknown input:

| Check | `None` input → |
| --- | --- |
| `check_reconciliation_lock` | `LOCK_ACCOUNT` |
| `check_broker_connectivity` | `LOCK_ACCOUNT` (`False` → `STOP_NEW_ORDERS`) |
| `check_daily_loss` | `LOCK_ACCOUNT` |
| `check_data_staleness` | `LOCK_ACCOUNT` |
| `check_position_exposure` / `check_gross_exposure` | conservative |

`risk_kill` imports nothing outside the standard library (enforced by
`tests/test_architecture.py`). The logic is correct and genuinely fail-closed.

**But it has no persistence.** `RiskGuard` is a pure function; `evaluate` is called per
invocation. Nothing writes its result anywhere durable.

⇒ **The deterministic guard's state does not survive a restart.**

### 6b. Operator switch — `datahub/state.py`

`var/system_state.json` → `{heartbeats, kill_switch: {armed, armed_at, reason, armed_by,
disarmed_at}, history[200]}`. Written atomically (tmp + `replace`), guarded by an `RLock`.
Heartbeats report `"never"` rather than a fake healthy value — this is well built.

**But it is consulted by only three call sites:**

```
dashboard/operations.py:255, 300
dashboard/server.py:484  (POST /api/kill-switch)
paper_trading/service.py:657, 684, 917
```

`execution/service.py` and `orchestration/pipeline.py` **never call `is_killed()`**.

⇒ **Arming the kill switch does not stop orchestrated execution.** (AUDIT-021.)

### 6c. `risk_kill/state.json`

Ships a hand-written document (`{"status": "ARMED", "tripped": false, …}`). `grep` shows it is
referenced only by `dashboard/main_dashboard.py` (the Streamlit dashboard, **not** the one
`dashboard/server.py` serves) and `scripts/generate_sample_data.py`.

⇒ **Dead, and worse than dead: it implies a persistence layer that does not exist.**

### Consolidated transition diagram (as it actually is)

```
                 ┌──────────────────── RiskGuard.evaluate(context) ─────────────────────┐
                 │  per call, in-memory, NOT persisted, survives nothing                 │
                 │  NOMINAL ─ ALERT_HUMAN ─ STOP_NEW_ORDERS ─ CANCEL_OPEN_ORDERS         │
                 │      └─ FLATTEN_POSITIONS ─ LOCK_ACCOUNT                              │
                 └───────────────────────────┬───────────────────────────────────────────┘
                                             │ execution/service.py, orchestration/pipeline.py
                                             ▼
                                   run proceeds or halts

   POST /api/kill-switch ──► datahub.state (var/system_state.json)  [PERSISTED]
                                             │
                     ┌───────────────────────┴────────────────────────┐
                     ▼                                                ▼
        paper_trading/service.py                      execution/ · orchestration/
        (rebalance + automation STOP)                 (NO EFFECT — never consulted)
                     │
                     ▼
        dashboard/live demo bot (STOP)              risk_kill/state.json (NEVER READ)
```

**Recommended:** one `KillSwitch` authority. `ExecutionService.execute_targets` and
`DailyPipeline.run_day` must call it before doing anything else and return the same fail-closed
summary used for `LOCK_ACCOUNT`. Persist `RiskGuard` outcomes (or delete `risk_kill/state.json`).
Patch in `FIX_PLAN.md` — **not applied**, because it changes a safety control.

---

## 7. RESEARCH GATE

**States:** `research/gate.py::GateVerdict` = `PASS | FAIL | FRAGILE | INSUFFICIENT_EVIDENCE`.
`GateDecision.__post_init__` rejects any verdict outside the enum, and `GateCheck.status` is
constrained to `pass | warn | fail`. Illegal states are **not** representable — good.

**Transition rule:**

```
failed checks exist      -> FAIL
else warned checks exist -> FRAGILE
else                     -> PASS
```

with an early return to `INSUFFICIENT_EVIDENCE` when
`len(evidence_returns) < minimum_observations` (252).

**Nine checks:** `evidence_sufficiency`, `statistical_confidence` (DSR + bootstrap CI),
`benchmark_comparison` (win-rate ≥ 60% vs Buy&Hold/EqualWeight/InverseVol/Persistence),
`cost_robustness` (cost share ≤ 50% of gross, 2×-cost Sharpe > 0), `drawdown_control`
(≥ −30%), `turnover_control` (≤ 8×/yr), `validation_consistency`, `placebo_dominance`,
and reproducibility metadata.

**Three ways the barrier leaks (AUDIT-032):**

1. `trials = config.tested_variants or (len(benchmarks) + len(placebo_results or {}) + 1)`.
   With the default `tested_variants=None` and no placebos, `trials = 1` and
   `expected_maximum_sharpe(1) = 0.0` — the DSR degenerates to an **uncorrected** single-trial
   test while the docstring claims multiple-testing correction. Only
   `research/candidate_set.py` sets `tested_variants` (AUDIT-038).
2. `validation is None` → a `warn` check → verdict `FRAGILE`, not `FAIL`. A strategy with
   **zero** walk-forward or CPCV evidence is one step from `PASS`.
3. `evidence_returns = oos_returns if oos_returns is not None else returns` — when the caller
   omits `oos_returns`, the gate grades the **in-sample** full backtest and labels it
   "out-of-sample evidence" in its own output.

There is also **no minimum trade-count check**, so a strategy with three trades can pass.

**Recommended:** require `tested_variants`; make missing validation a `fail`; require
`oos_returns` (or rename the reported field). Patch in `FIX_PLAN.md` — **not applied**, it is a
gating-policy decision.

---

## 8. AUTH

**States:** a `TokenRecord` (broker, token, `expires_at`) persisted by
`broker.token.FileTokenStore` with **owner-only file permissions**.

**Transitions / guards:**

| Event | Behaviour |
| --- | --- |
| OAuth | `login_url` produces a sandbox URL; `complete_login` exchanges a **manually pasted** code. Login is never automated. |
| read token | `TokenManager.get_token` → raises `StaleTokenError` when expired/absent |
| use token | `SandboxExecutionAdapter` converts `StaleTokenError` into a deterministic `REJECTED` result **before** any transport call |
| display | `masked_token` — the dashboard shows `broker_health.token.masked`, never the raw value |
| expiry | `seconds_until_expiry`, `is_expired`; `dashboard/operations.py` reports `state = TOKEN_EXPIRED` |

Verified live with no token: `{"configured": false, "state": "NOT_CONFIGURED", "token": null}`,
and the quote chain degrades to clearly-labelled `SIM` quotes rather than failing.

**No defects found.** This machine and the broker-mode machine are the two genuinely
production-grade parts of the system.

---

## Cross-machine observations

1. **Two risk-limit vocabularies** (AUDIT-024). `risk_kill.RiskLimits`:
   max position 25%, gross 100%, daily loss 3%, drawdown **10%**, data age **18 h**.
   `paper_trading.DEFAULT_RISK_POLICY`: max position **15%**, gross 100%, daily loss 3%,
   drawdown **15%**. Nothing defines which governs, and the paper path never consults
   `risk_kill` at all.
2. **The research gate does not gate paper trading.** `preview_rebalance` checks
   `is_paper_approved(strategy_id)` against a JSON registry — a **manually maintained file** —
   not against a `GateDecision`. There is no automated link from `PASS` to
   `paper_approved: true`.
3. **No machine owns "the system is halted" holistically.** `observability.health.SystemHealth`
   and `risk_kill.RiskState` are separate vocabularies that were conflated in
   `execution/service.py` (AUDIT-001, now fixed via `risk_kill/mapping.py`).
