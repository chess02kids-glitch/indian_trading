# Quant India — Dashboard & Full Automation Control Plane (v1)

**This document is the source of truth for the dashboard implementation.**

This extends the v3 production plan. It doesn't replace anything there — it fills in the piece you asked for by name (the dashboard) and wires it to every component already specced (data, research gate, risk engine, kill switch, execution, reconciliation) into one control plane you actually look at and operate.

## Table of Contents

1. [Diagnostic Protocol — run this before building anything else](#0-diagnostic-protocol--run-this-before-building-anything-else)
2. [Where the Dashboard Sits](#1-where-the-dashboard-sits)
3. [Dashboard Screens](#2-dashboard-screens)
4. [Automation / Orchestration Wiring](#3-automation--orchestration-wiring)
5. [The Go-Live Gate](#4-the-go-live-gate--implemented-as-ui-not-just-policy)
6. [Minimal Streamlit skeleton](#5-minimal-streamlit-skeleton-illustrative)
7. [What I'd actually do next, in order](#6-what-id-actually-do-next-in-order)

---

## 0. Diagnostic Protocol — run this before building anything else

**IMPLEMENTATION: `diagnostics/protocol.py`**

Add a `diagnostics/` folder and run these, in order, against your existing harness:

1. **Buy-and-hold sanity check.** Backtest a pure long Nifty 50 position over your test window. Compare CAGR/max-drawdown against the index's known real history for that period. Mismatch → your harness has a bug, stop and fix it before re-testing any strategy.
2. **Zero-cost pass.** Re-run all 20 with slippage and brokerage set to zero. If *none* show even a raw, pre-cost edge, that's a different finding than "costs killed them" — it means the signal itself doesn't exist in your data, which points at parameters, universe, or lookback bugs rather than cost realism.
3. **Look-ahead check.** For each strategy, confirm the signal computed at bar `t` only uses data available *as of* `t` (no same-bar close used to trigger a same-bar entry unless your execution model explicitly accounts for that). This is the single most common silent bug in home-built backtesters.
4. **Universe check.** Confirm delisted/renamed stocks are still in your historical universe for the periods they were listed. A survivorship-biased universe should make results look *better* than reality — if you're still failing despite this bias working in your favor, that's a meaningful signal in favor of "no edge," not "bad luck."
5. **Cost breakdown, not just net P&L.** Log gross P&L and total cost drag separately for each strategy. A strategy with positive gross P&L that costs kill is a different problem (lower turnover, widen stop distance, revisit instrument liquidity) than a strategy with negative gross P&L (the signal itself is wrong).

**Log the outcome of steps 1-5 as `HYP-DIAG-001` in MLflow alongside the 20 original runs — this is itself research-ledger data, not throwaway debugging.**

**Run with:**
```bash
python diagnostics/run_diagnostics.py
```

---

## 1. Where the Dashboard Sits

```
                    ┌─────────────────────────────────────────┐
                    │        DASHBOARD (Streamlit)             │
                    │  reads-only from everything below;       │
                    │  writes only to: pause/resume, promote/  │
                    │  demote confirmation, manual kill switch  │
                    └───────────────┬───────────────────────────┘
                                    │ reads
        ┌───────────────┬──────────┴──────────┬───────────────┬────────────────┐
        ↓               ↓                     ↓               ↓                ↓
  health_check.json  mlflow (research   risk_kill/state    execution/     reconciliation/
  (data freshness,    ledger, DSR,      (kill-switch       order log,     (EOD diffs,
  broker heartbeat,   gate status)      status, active     fill status    mismatch flags)
                                       halts)
```

The dashboard is deliberately **read-mostly**. It is not where trading logic lives, and it is not where the kill switch's decisions get made — it's the window onto a system whose actual decisions are made by `risk_kill/` and the orchestration pipeline, exactly as designed in v3. The two write actions it's allowed (manual kill switch, promote/demote confirmation) are both *more restrictive* actions a human can take, never less restrictive ones.

**IMPLEMENTATION:**
- `dashboard/main_dashboard.py` - Primary Streamlit dashboard
- `observability/health.py` - HealthService for system state
- `risk_kill/guard.py` - RiskGuard for kill switch state

---

## 2. Dashboard Screens

### Screen 1 — Command Center (the one you check every morning)

**IMPLEMENTATION: `render_command_center()` in `main_dashboard.py`**

- **System state banner**: `RESEARCH` / `PAPER` / `LIVE — ₹X stage` / `HALTED (reason)` / `LOCKED (manual review required)` — one word, top of screen, colored (green/amber/red)
- Last successful pipeline run: timestamp, duration, stage-by-stage pass/fail
- Data freshness: age of last ingested bar per source
- Broker connectivity heartbeat: last successful ping, latency
- Kill-switch status: `ARMED — no trips` or `TRIPPED: <condition> at <time>`
- Today's realized + unrealized P&L (only rendered once any capital is live; shows "—" during research/paper)

### Screen 2 — Research Pipeline

**IMPLEMENTATION: `render_research_pipeline()` in `main_dashboard.py`**

- Table of every hypothesis tried (from MLflow), not just survivors: ID, strategy family, date range tested, Sharpe (raw), Sharpe (cost-adjusted), Deflated Sharpe, CPCV pass/fail, status (`rejected` / `paper` / `live` / `demoted`)
- A single, unmissable **gate-status card** per candidate strategy: five checkmarks (beats all 4 baselines / DSR positive / CPCV pass / survives pessimistic costs / live-paper divergence within tolerance) — all five must be green before anything below unlocks
- Count of variants tested (feeds directly into why DSR looks the way it does — shown so you never lose track of your own multiple-testing exposure)

### Screen 3 — Risk & Kill Switch

**IMPLEMENTATION: `render_risk_kill_switch()` in `main_dashboard.py`**

- Every hard limit from `risk_kill/`, its current value vs. its threshold, as a live gauge (daily loss used / daily loss limit, current drawdown / max drawdown, orders this second / rate cap)
- Kill-switch trip history: timestamp, condition, response level (`STOP_NEW_ORDERS` / `CANCEL_OPEN_ORDERS` / `FLATTEN_POSITIONS` / `LOCK_ACCOUNT`), whether resolved
- **Manual kill switch** — a single, deliberately inconvenient control (type-to-confirm the stage name) that immediately sets `STOP_NEW_ORDERS`, always available regardless of system state

### Screen 4 — Capital Ladder

**IMPLEMENTATION: `render_capital_ladder()` in `main_dashboard.py`**

- Current stage (₹0 → ... → ₹1,00,000), visualized as a literal ladder with the current rung highlighted
- Promotion criteria checklist for the *next* rung, live-evaluated, greyed out until all are true
- Demotion history: which stage, which breach, when re-promoted
- **This screen contains the actual "go live" control**, described in §4 below — nowhere else in the dashboard can live trading be enabled

### Screen 5 — Reconciliation & Broker Health

**IMPLEMENTATION: `render_reconciliation()` in `main_dashboard.py`**

- EOD position diff: DuckDB-recorded vs. broker-reported, per instrument, highlighted red on any mismatch
- Order-level log: internal ID, idempotency key, broker order ID, submitted/filled/rejected/partial, expected vs. actual price
- Token/session status: time until current broker access token expires, last re-auth timestamp (surfaces the daily-token-expiry constraint from v3 directly in the UI so it's never a surprise)

### Screen 6 — Alerts & Incident Log

**IMPLEMENTATION: `render_alerts()` in `main_dashboard.py`**

- Full Telegram alert history, filterable by severity (info/warning/critical)
- One-page incident template auto-populated on any `LOCK_ACCOUNT` event: what tripped, system state at the time, positions at the time, manual actions taken — this is the "written incident plan" item from the v3 checklist, made structural instead of aspirational

---

## 3. Automation / Orchestration Wiring

**IMPLEMENTATION: `orchestration/pipeline.py`**

```
systemd timer (VPS, IST market-hours-aware schedule)
        ↓
orchestration/daily.py
        ↓
 1. re-auth (OAuth + 2FA — manual tap or confirmed-safe automated flow, per v3 §10)
 2. ingest → validate → staleness check           → write health_check.json after each stage
 3. generate signals (only if a strategy has cleared the gate; otherwise pipeline stops here
    and dashboard shows RESEARCH/PAPER state — no live signal generation happens for
    ungated strategies, this is enforced in code, not by convention)
 4. risk_kill/ pre-trade checks
 5. place orders (LIMIT only, rate-capped) — only reachable if step 3 produced a live signal
    AND step 4 passed AND Screen 4's live-trading toggle is enabled
 6. poll fills → reconciliation → update DuckDB
 7. EOD reconciliation vs. broker
 8. write final health_check.json (status: success/failure/halted)
 9. Telegram summary (severity-tagged)
        ↓
dashboard reads health_check.json + DuckDB + MLflow + risk_kill state on every page load
```

Every stage writes its own status to `health_check.json` incrementally, not just at the end — if the pipeline dies at step 4, the dashboard shows exactly that, not a stale "last successful run" from three days ago.

---

## 4. The Go-Live Gate — implemented as UI, not just policy

**IMPLEMENTATION: `live_trading_unlockable()` and `render_go_live_gate()` in `main_dashboard.py`**

This is the direct answer to "help me invest real money": the architecture fully supports it, and the switch to flip is on Screen 4 — but it's **programmatically disabled** until every condition below reads `true` from the research ledger, not from your own judgment call in the moment:

```python
def live_trading_unlockable(strategy_id: str) -> tuple[bool, list[str]]:
    checks = {
        "beats_all_baselines": ...,      # from backtest/
        "deflated_sharpe_positive": ..., # from research gate calc
        "cpcv_pass": ...,                # from research gate calc
        "survives_pessimistic_costs": ...,
        "live_paper_divergence_ok": ..., # paper Sharpe within ~30% of backtest Sharpe
        "min_paper_trading_days": ...,   # e.g. >= 20 trading days paper, no override
    }
    unlockable = all(checks.values())
    return unlockable, [k for k, v in checks.items() if not v]
```

Screen 4 renders the failing checks directly next to the greyed-out toggle — so at any point, you can see exactly what's still missing, not just "not allowed yet." Once a strategy passes, the toggle becomes clickable, still requires the type-to-confirm step, and only then does step 5 in §3 become reachable for that strategy. Nothing about this blocks you from *building or running* the full system today — paper trading, reconciliation, dashboard, alerts, the daily automated pipeline can all be live right now. It blocks capital specifically from reaching a strategy that hasn't earned it, which is the one property this whole document exists to protect.

---

## 5. Minimal Streamlit skeleton (illustrative)

**FULL IMPLEMENTATION: `dashboard/main_dashboard.py`**

```python
import streamlit as st, json, duckdb, mlflow

st.set_page_config(page_title="Quant India — Command Center", layout="wide")
health = json.load(open("observability/health_check.json"))

state_color = {"RESEARCH": "blue", "PAPER": "orange", "LIVE": "green", "HALTED": "red", "LOCKED": "red"}
st.markdown(f"### System state: :{state_color.get(health['state'],'gray')}[{health['state']}]")

col1, col2, col3 = st.columns(3)
col1.metric("Last run", health["last_run_ts"])
col2.metric("Data freshness (min)", health["data_age_min"])
col3.metric("Broker heartbeat (min ago)", health["broker_last_ping_min"])

# Screen 4 — the gate, rendered explicitly
unlockable, failing = live_trading_unlockable(st.session_state.get("strategy_id"))
st.subheader("Go-live gate")
for check_name in ["beats_all_baselines","deflated_sharpe_positive","cpcv_pass",
                    "survives_pessimistic_costs","live_paper_divergence_ok","min_paper_trading_days"]:
    st.checkbox(check_name, value=check_name not in failing, disabled=True)

st.button("Enable live trading", disabled=not unlockable,
          help="Disabled until every check above is green — no manual override.")
```

This is a starting skeleton, not a finished app — flesh out each screen from §2 against your actual `health_check.json` schema and DuckDB tables as you build.

---

## 6. What I'd actually do next, in order

1. ✅ **Run the diagnostic protocol in §0** — this is higher-value right now than any dashboard work
   ```bash
   python diagnostics/run_diagnostics.py
   ```
2. ✅ **Log the diagnostic result as its own hypothesis in MLflow**
3. If it's a harness bug: fix it, re-run all 20, see what survives with a correct harness
4. If it's genuinely "no edge after costs": that's useful — it tells you off-the-shelf retail strategies aren't your edge, and pushes you toward either lower-turnover asset-allocation-style approaches (§24 of the v3 plan) or toward differentiated research rather than well-known public strategies everyone's already arbitraged away
5. ✅ **Build the dashboard against paper trading first** — you'll want it working and trustworthy long before it matters that it's gating real capital
   ```bash
   python scripts/generate_sample_data.py
   streamlit run dashboard/main_dashboard.py
   ```
6. Only then does Screen 4's toggle become the interesting part of your day

---

## Implementation Status

| Component | Status | Location |
|-----------|--------|----------|
| Diagnostic Protocol | ✅ Complete | `diagnostics/protocol.py` |
| Run Script | ✅ Complete | `diagnostics/run_diagnostics.py` |
| Main Dashboard | ✅ Complete | `dashboard/main_dashboard.py` |
| Command Center (Screen 1) | ✅ Complete | `render_command_center()` |
| Research Pipeline (Screen 2) | ✅ Complete | `render_research_pipeline()` |
| Risk & Kill Switch (Screen 3) | ✅ Complete | `render_risk_kill_switch()` |
| Capital Ladder (Screen 4) | ✅ Complete | `render_capital_ladder()` |
| Reconciliation (Screen 5) | ✅ Complete | `render_reconciliation()` |
| Alerts & Incidents (Screen 6) | ✅ Complete | `render_alerts()` |
| Go-Live Gate | ✅ Complete | `live_trading_unlockable()` |
| Sample Data Generator | ✅ Complete | `scripts/generate_sample_data.py` |
| Documentation | ✅ Complete | `DASHBOARD_SPEC.md`, README files |

---

## File Structure

```
indian_trading/
├── dashboard/
│   ├── __init__.py              # Module exports
│   ├── main_dashboard.py        # Primary Streamlit dashboard (NEW)
│   ├── README.md                # Dashboard documentation
│   └── ... (legacy dashboards)
│
├── diagnostics/
│   ├── __init__.py
│   ├── protocol.py              # Diagnostic protocol implementation
│   ├── run_diagnostics.py       # CLI entry point
│   └── README.md
│
├── observability/
│   ├── health.py                # HealthService
│   └── ...
│
├── risk_kill/
│   ├── guard.py                 # RiskGuard
│   └── ...
│
├── orchestration/
│   └── pipeline.py              # Daily pipeline
│
└── scripts/
    └── generate_sample_data.py # Sample data for testing
```

---

## Quick Start Commands

```bash
# 1. Install dependencies
make install

# 2. Generate sample data for testing
make generate-sample-data

# 3. Run diagnostics
make diagnostics

# 4. Run the dashboard
make dashboard

# 5. Run the server
make server
```

---

## Key Design Principles

1. **Read-Mostly Architecture**: The dashboard reads from existing systems (health_check.json, MLflow, risk_kill/, execution/, reconciliation/). It does not contain trading logic.

2. **Fail-Closed by Default**: All write actions (manual kill switch, go-live toggle) are more restrictive, never less restrictive. The system defaults to safety.

3. **Programmatic Gates**: The go-live gate is programmatically enforced. No manual override can bypass the required checks.

4. **Incremental Status Updates**: Every pipeline stage writes its status incrementally. The dashboard always shows the current, accurate state.

5. **Diagnostics First**: Validate your harness before building the dashboard. A buggy harness will produce meaningless results regardless of dashboard quality.

---

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        QUANT INDIA SYSTEM                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │   Data       │    │  Research     │    │   Risk       │      │
│  │  Ingestion   │───▶│   Pipeline    │───▶│   Engine     │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│          │                   │                    │              │
│          ▼                   ▼                    ▼              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│  │ health_check │◀───│  MLflow       │◀───│ risk_kill/    │      │
│  │ .json        │    │  (ledger)     │    │ state         │      │
│  └──────────────┘    └──────────────┘    └──────────────┘      │
│          ▲                   ▲                    ▲              │
│          │                   │                    │              │
│          └───────────────────┼────────────────────┘              │
│                              │                                      │
│                              ▼                                      │
│                    ┌─────────────────────┐                         │
│                    │      DASHBOARD       │                         │
│                    │   (Streamlit)        │                         │
│                    │                     │                         │
│                    │  6 Screens:          │                         │
│                    │  1. Command Center   │                         │
│                    │  2. Research Pipe.   │                         │
│                    │  3. Risk & Kill Sw.  │                         │
│                    │  4. Capital Ladder  │◀── Go-Live Gate          │
│                    │  5. Reconciliation  │                         │
│                    │  6. Alerts & Inc.    │                         │
│                    └─────────────────────┘                         │
│                                                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| v1 | 2026-08-30 | Initial implementation with all 6 screens and go-live gate |

---

## References

- v3 Production Plan (referenced but not included in this document)
- Section 24 of v3 plan (lower-turnover asset-allocation approaches)
