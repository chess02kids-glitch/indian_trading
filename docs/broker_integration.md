# Broker Sandbox Integration (v0.4)

This document describes the broker sandbox layer delivered in v0.4. It
enables safe broker round-trips against **sandbox environments only**, while
preserving the paper-trading architecture. **Live trading is not enabled.**

## Architecture

The flow is unchanged — the broker layer slots in behind the risk gate:

```
Research
    ↓
Portfolio
    ↓
Order Intent (models.domain.OrderIntent — LIMIT-only by construction)
    ↓
Risk Engine (risk_kill.RiskGuard — untouched)
    ↓
broker.safe_execution.SandboxExecutionAdapter   ← safe execution layer
    ↓
broker.adapter.BrokerAdapter                    ← unified broker interface
    ↓
SandboxBroker (broker.simulated.SimulatedBrokerBackend)
```

Dependency rules (enforced by `tests/test_broker_architecture.py`):

* `risk_kill` imports nothing outside stdlib (+ `models`) and is untouched.
* `research`, `portfolio`, `backtest`, `agents` never import `broker` or
  `auth` — research code cannot call a broker.
* `execution` never imports `broker` — the sandbox executor is injected via
  the existing `ExecutionAdapter` protocol (`submit_order`, `cancel_order`,
  `get_order_status`, `get_positions`, `get_open_orders`).
* `broker` imports no network clients, no real broker SDKs, and no
  research-side packages.

## Unified broker interface (`broker.interface.BrokerAdapter`)

authentication (`login_url` / `complete_login` / `is_authenticated`),
`ping`, profile, funds, holdings, positions, quotes, `place_limit_order`,
`get_order_status`, `cancel_order`, `get_trade_history`.

Implementations: `UpstoxAdapter` and `DhanAdapter` (both sandbox-only),
mirroring the identical interface over their own wire dialects
(Upstox v2 `{"status", "data"}` snake-case envelopes vs Dhan v2 bare
camelCase objects and different status strings). They are fully
interchangeable at the domain level — a policy test proves identical
outcomes for identical inputs.

## Feature flags / execution modes

`QUANT_EXECUTION_MODE` selects the operating mode:

| Mode       | Behaviour                                             |
|------------|-------------------------------------------------------|
| RESEARCH   | Research only. Broker reads refused; execution refused.|
| PAPER      | Existing paper-trading path (unchanged).              |
| SANDBOX    | Sandbox broker round-trips via the broker layer.      |
| LIVE       | **Disabled.** Refused at every gate, always.           |

`LIVE` exists as an explicit enum value in `broker.mode.OperatingMode` so
the refusal is testable code, not an absent value. The domain
`models.domain.ExecutionMode` intentionally has no `LIVE` member
(architecture invariant, unchanged). Refusal points:

1. `broker.mode.check_execution_permitted` — raises `LiveTradingDisabledError`.
2. Adapter construction in LIVE mode raises `LiveTradingDisabledError`.
3. `validate_sandbox_base_url` — only `simulated://`, loopback, and
   `sandbox.*`/`api-sandbox.*` hosts are accepted; any production URL fails
   construction.
4. The safe execution layer re-checks the mode at submission time.
5. The broker CLI refuses every command when the env declares `LIVE`.

## Safe execution layer

`SandboxExecutionAdapter.submit_order` enforces, in order, *before* broker
submission:

1. Mode gate (LIVE/RESEARCH refuse).
2. `execution.validation.validate_order_intent` — **LIMIT-only**: MARKET and
   IOC are rejected (never converted).
3. Duplicate prevention — a repeated idempotency key returns the stored
   result; nothing reaches the broker twice. The sandbox backend also
   deduplicates by client order id (tag).
4. Limit-price band (`validate_limit_price_band`, default ±10%).
5. **Rate limiter** — default 1 order/second, FIFO queueing through a pacing
   lock; clock/sleeper injectable for determinism.
6. **Token gate** — client-side expiry detection (`TokenManager.get_token`)
   turns a stale token into a deterministic `REJECTED` result before any
   broker call. Broker-side 401 analogues are mapped to `REJECTED` too.
7. Submission with **exponential retry** on transient transport faults only
   (0.25s, 0.5s, 1s … injected sleeper). Exhaustion yields `UNKNOWN` — the
   order state is genuinely unknown; the in-flight idempotency claim blocks
   blind resubmission, and reconciliation resolves it.

## Daily token management (`broker.token`)

* `TokenRecord` — issued/expiry times; `FileTokenStore` writes owner-only
  (`0600`) files atomically.
* Expiry tracking: `TokenManager.status` → `active` / `expiring_soon` /
  `expired` / `missing` (never leaks raw tokens — masked).
* Expiry detection: `get_token` raises `StaleTokenError` when missing or
  expired — used as the pre-submission gate above.
* Refresh scheduling: `refresh_due_at` / `reauth_schedule` return the
  wall-clock moment a human should re-authenticate (expiry − 30 min margin).
* Manual re-auth workflow: `begin_reauth` prepares a login URL + state;
  `complete_reauth` stores the token obtained from a human-supplied code.
  **Login is never automated** (ADR-009): no headless browser, no scheduled
  auto-login; the framework only prepares and tracks the workflow.

## Sandbox reconciliation (`broker.reconciler.SandboxReconciler`)

* `poll_order` — bounded, deterministic status polling after every sandbox
  order (transient timeouts consume one poll attempt, then resume).
* `sync_order` — polls + persists the latest result into the local order
  repository and syncs the affected position (reconcile fills → update local
  state).
* `reconcile_now` / `end_of_day` — diffs expected (local repositories) vs
  actual (broker) state through the existing
  `reconciliation.engine.ReconciliationEngine`: positions, open orders,
  duplicates, and fills. Any mismatch **locks the account** and maps to a
  `LOCK_ACCOUNT` risk decision (ADR-008 — reconciliation is a kill switch).
  End-of-day reconciliation remains mandatory.

## Failure injection (`broker.simulated` faults)

Scripted deterministically via `SimulatedSandboxTransport.script(action,
faults)` (FIFO per action, `"*"` for all actions):

| Fault                    | Behaviour                                            |
|--------------------------|------------------------------------------------------|
| `TimeoutFault`           | Request times out; no broker-side change.            |
| `DisconnectFault`        | Connection drops once; next request reconnects.      |
| `StaleTokenFault`        | Broker rejects the token once (401 analogue).        |
| `RejectFault(reason)`    | Order created but rejected by the broker.            |
| `PartialFillFault(frac)` | Order partially fills `frac` of the quantity.        |
| `PendingFault(polls)`    | Order stays open; fills after `polls` status checks. |

Retries consume subsequent scripted faults, so exact failure sequences are
replayable — `tests/test_sandbox_failures.py` proves byte-identical replays.

## Observability

* **Streamlit** `dashboard/broker_dashboard.py` (read-only; no execution
  controls): broker connectivity, token status, sandbox health,
  reconciliation health, recent sandbox orders. Data access via
  `dashboard/broker_status.py` — a missing/malformed status file renders
  everything as `unknown`.
* **Status document** `var/broker_status.json` (override
  `QUANT_BROKER_STATUS_FILE`) written by the CLI / jobs.

## CLI (`python main.py broker …`)

```
broker health      [--broker upstox|dhan|all]
broker login       <broker> [--code CODE]   # manual OAuth skeleton
broker funds       <broker>                 # read-only
broker holdings    <broker>                 # read-only
broker positions   <broker>                 # read-only
broker orders      <broker>                 # recent sandbox orders
broker sandbox-order  <broker> --symbol X --side BUY --quantity N --limit-price P
broker sandbox-cancel <broker> --internal-id ID
broker reconcile   <broker>                 # mandatory end-of-day check
```

`sandbox-order` runs the full chain: mode gate → intent validation → risk
guard (built from live funds/positions/quotes/connectivity) → duplicate
prevention → rate limiter → token gate → sandbox broker, then persists the
status document. Exit codes: 0 ok · 1 error · 2 rejected/unknown ·
3 refused (mode, risk guard, or reconciliation lock).

## Configuration

| Variable                       | Default                              |
|--------------------------------|--------------------------------------|
| `QUANT_EXECUTION_MODE`         | RESEARCH (library) / SANDBOX (CLI)   |
| `QUANT_DATA_DIR`               | `data` (holds `broker_tokens/`, `broker_sandbox/`) |
| `QUANT_BROKER_STATUS_FILE`     | `var/broker_status.json`             |

## Limitations (by design)

* **No production credentials, no live HTTP**: the only functional transport
  is the deterministic in-process simulator. `HttpSandboxTransportStub`
  proves the URL policy but performs no requests in this build.
* Upstox has no official public sandbox; the environment modelled here is
  our deterministic simulation of its sandbox semantics, not a vendor
  endpoint. Dhan semantics are modelled the same way.
* The simulated backend persists to local JSON — it is not multi-process
  safe and is not a market simulator (fills happen at limit price; there is
  no order book).
* The broker CLI reconciles against the orders recorded in its own status
  document; programmatic use injects real repositories into
  `SandboxReconciler` for full reconciliation.
* Real OAuth token exchange, token encryption at rest beyond the local
  `0600` files, and vendor SDK mapping remain future work and require a new
  ADR + human sign-off (see `docs/GO_LIVE_CHECKLIST.md`).
