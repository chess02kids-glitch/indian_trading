# ADR-011: Broker Sandbox Layer (v0.4)

## Context
After v0.3 (scientific validation + production hardening, paper trading with
Supabase transactional layer), the next step towards eventual execution is
proving broker round-trips — authentication, orders, fills, cancellation,
reconciliation — without touching production capital. Indian brokers'
sandbox setups are inconsistent (Upstox has no official public sandbox;
Dhan's sandbox is a stub environment), so a deterministic local simulation
of sandbox semantics is the only way to make the integration testable in CI.

## Decision
Build a dedicated `broker` package with:

1. **Unified adapter interface** (`broker.interface.BrokerAdapter`) covering
   authentication skeleton, profile, funds, holdings, positions, quotes,
   order placement, order status, cancellation, and trade history. Shipped
   as `UpstoxAdapter` and `DhanAdapter`, both sandbox-only and
   interchangeable, each over its own wire dialect.
2. **Feature-flag execution modes** `RESEARCH / PAPER / SANDBOX / LIVE`
   where `LIVE` is a permanently disabled enum value refused at
   construction, URL validation, submission, and CLI level. The domain
   `ExecutionMode` enum remains without a `LIVE` member.
3. **Safe execution layer** (`SandboxExecutionAdapter`) bridging the broker
   adapters to the existing `ExecutionAdapter` protocol, enforcing
   LIMIT-only validation, duplicate prevention (client idempotency map +
   broker-side tag dedup), a 1 order/second pacing rate limiter with queue,
   exponential retry on transient faults, and client-side token-expiry
   gates — all before broker submission.
4. **Deterministic simulated broker** (`SimulatedBrokerBackend` +
   `SimulatedSandboxTransport`) with JSON persistence and scripted failure
   injection (timeout, rejection, partial fill, duplicate, stale token,
   disconnect/reconnect). The `HttpSandboxTransportStub` documents the seam
   for a future real sandbox HTTP client and refuses by default.
5. **Token management framework** (`broker.token`) for daily expiry
   tracking, refresh scheduling, expiry detection, and manual re-auth
   workflow — login is never automated (reaffirms ADR-009).
6. **Sandbox reconciliation** reusing the existing
   `reconciliation.engine.ReconciliationEngine` after every sandbox order
   (poll/status sync) and mandatorily at end-of-day; mismatches lock the
   account (reaffirms ADR-008).

## Alternatives Considered
- **Direct vendor SDK integration (upstox-python, dhanhq)**: Rejected —
  adds heavy third-party dependencies, pulls network code into the repo, and
  cannot be exercised deterministically in CI. The adapter interface is
  designed so a vendor SDK can later be wrapped behind it with a new ADR.
- **Live-capable adapter behind a compile-time flag**: Rejected — any code
  path to production capital, however gated, violates the project boundary.
  Refusal at multiple independent layers is stronger than a single flag.
- **New sandbox-specific reconciliation engine**: Rejected — reconciliation
  is a kill switch (ADR-008); a second implementation would dilute it. The
  sandbox reuses the existing engine.

## Consequences
- **Pros**: full broker round-trip proven hermetically (auth → order → fill
  → reconcile → lock on drift); paper mode untouched; risk-kill untouched;
  every failure mode deterministic and replayable; dashboards and CLI
  surfaces for operations.
- **Cons**: the simulated backend is a semantics model, not a vendor
  endpoint; wire-dialect mapping for real APIs remains to be verified
  against real sandboxes when a future phase is approved.

## Future Review Criteria
If a vendor sandbox becomes reliably available, wrap it as a new
`SandboxTransport` implementation behind the existing adapter interface,
keeping the simulated transport for CI. Live trading still requires a new
ADR, human sign-off, and the GO_LIVE_CHECKLIST.
