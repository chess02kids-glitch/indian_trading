# Quant India Dashboard — v1

**Dashboard & Full Automation Control Plane**

This module implements the complete dashboard specification from Section 2, with all 6 screens and the go-live gate from Section 4.

## Architecture

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

## Quick Start

### 1. Generate Sample Data

Before running the dashboard, generate sample data for testing:

```bash
python scripts/generate_sample_data.py
```

This creates sample files in all expected locations.

### 2. Run the Dashboard

```bash
streamlit run dashboard/main_dashboard.py
```

The dashboard will be available at `http://localhost:8501`.

### 3. Run Diagnostics (Recommended First)

Before building anything else, run the diagnostic protocol:

```bash
python diagnostics/run_diagnostics.py
```

This implements Section 0 and logs results as HYP-DIAG-001.

## Dashboard Screens

### Screen 1 — Command Center
The one you check every morning.

**Features:**
- System state banner (RESEARCH / PAPER / LIVE / HALTED / LOCKED)
- Last successful pipeline run timestamp and duration
- Data freshness: age of last ingested bar per source
- Broker connectivity heartbeat: last successful ping, latency
- Kill-switch status: ARMED or TRIPPED with condition
- Today's realized + unrealized P&L (only when capital is live)
- Diagnostic protocol status (HYP-DIAG-001)

### Screen 2 — Research Pipeline
All hypotheses tried, not just survivors.

**Features:**
- Table of every hypothesis from MLflow
- ID, strategy family, date range, Sharpe (raw/cost-adjusted), Deflated Sharpe
- Status (rejected / paper / live / demoted)
- Gate-status card with five checkmarks:
  - Beats all 4 baselines
  - DSR positive
  - CPCV pass
  - Survives pessimistic costs
  - Live-paper divergence within tolerance
- Count of variants tested

### Screen 3 — Risk & Kill Switch
Live risk monitoring and manual override.

**Features:**
- Live gauges for all hard limits:
  - Daily loss used / daily loss limit
  - Current drawdown / max drawdown
  - Orders this second / rate cap
  - Position concentration / concentration limit
- Kill-switch trip history with timestamps and conditions
- Manual kill switch (type-to-confirm the stage name)
- Immediately sets STOP_NEW_ORDERS
- Always available regardless of system state

### Screen 4 — Capital Ladder
Visual representation of capital allocation.

**Features:**
- Current stage visualized as a literal ladder
- Promotion criteria checklist (live-evaluated)
- Greyed out until all criteria are met
- Demotion history with breach details
- **The actual "go live" control** (nowhere else in the dashboard)
- Go-live gate implementation from Section 4

### Screen 5 — Reconciliation & Broker Health
Position and order verification.

**Features:**
- EOD position diff: DuckDB-recorded vs. broker-reported
- Red highlighting on any mismatch
- Order-level log with filtering:
  - Internal ID, idempotency key, broker order ID
  - Submitted/filled/rejected/partial status
  - Expected vs. actual price
- Token/session status:
  - Time until current broker access token expires
  - Last re-auth timestamp

### Screen 6 — Alerts & Incident Log
Full alert history and incident management.

**Features:**
- Telegram alert history (filterable by severity: info/warning/critical)
- One-page incident template auto-populated on LOCK_ACCOUNT events
- What tripped, system state at the time, positions at the time
- Manual actions taken

## Go-Live Gate Implementation

The go-live gate is implemented as UI, not just policy. It's programmatically disabled until every condition reads `true` from the research ledger:

```python
def live_trading_unlockable(strategy_id: str) -> tuple[bool, list[str]]:
    checks = {
        "beats_all_baselines": ...,  # from backtest/
        "deflated_sharpe_positive": ...,  # from research gate calc
        "cpcv_pass": ...,  # from research gate calc
        "survives_pessimistic_costs": ...,
        "live_paper_divergence_ok": ...,  # paper Sharpe within ~30% of backtest
        "min_paper_trading_days": ...,  # e.g. >= 20 trading days paper
    }
    unlockable = all(checks.values())
    return unlockable, [k for k, v in checks.items() if not v]
```

**Key Properties:**
- Screen 4 renders the failing checks directly next to the greyed-out toggle
- You can always see exactly what's still missing
- Once a strategy passes, the toggle becomes clickable
- Still requires type-to-confirm step
- Only then does execution become reachable for that strategy

## Data Requirements

The dashboard reads from these locations:

### Required Files
- `observability/health_check.json` - System health state
- `risk_kill/state.json` - Kill switch status
- `reports/generated/experiments/experiments.jsonl` - MLflow experiment data
- `reconciliation/results.json` - Reconciliation status
- `execution/orders.jsonl` - Order execution log
- `observability/alerts.jsonl` - Alert history

### Optional Files
- `var/diagnostics/HYP-DIAG-001.json` - Diagnostic protocol results
- `config/capital_ladder.json` - Capital ladder configuration

### File Formats

#### health_check.json
```json
{
  "state": "PAPER",
  "last_run_ts": "2026-08-30 10:30:00",
  "data_age_min": 5,
  "broker_last_ping_min": 2,
  "daily_loss_used": 0,
  "daily_loss_limit": 10000,
  "current_drawdown": 0.02,
  "max_drawdown_limit": 0.2,
  "orders_this_second": 0,
  "rate_cap": 10,
  "realized_pnl": 0,
  "unrealized_pnl": 0,
  "broker_health": {"connected": true, "latency_ms": 45},
  "reconciliation": {"matched": true, "locked": false},
  "kill_switch": {"status": "ARMED", "tripped": false}
}
```

#### risk_kill/state.json
```json
{
  "status": "ARMED",
  "tripped": false,
  "condition": null,
  "history": []
}
```

#### experiments.jsonl
Each line is a JSON object:
```json
{"hypothesis_id": "HYP-001", "strategy": "Momentum", "status": "live", "metrics": {"sharpe": 1.2, "total_return": 0.45}, "gate_result": {"verdict": "pass", "checks": [...]}}
```

## Configuration

### Capital Ladder
Edit `config/capital_ladder.json` to configure your capital stages:

```json
{
  "stages": [
    {"name": "Paper Trading", "amount": 0, "currency": "₹"},
    {"name": "Stage 1", "amount": 10000, "currency": "₹"},
    {"name": "Stage 2", "amount": 50000, "currency": "₹"},
    {"name": "Stage 3", "amount": 100000, "currency": "₹"}
  ],
  "current_stage": 0,
  "promotion_criteria": [
    "beats_all_baselines",
    "deflated_sharpe_positive",
    "cpcv_pass",
    "survives_pessimistic_costs",
    "live_paper_divergence_ok",
    "min_paper_trading_days"
  ]
}
```

## Automation Wiring

The dashboard integrates with the orchestration pipeline from `orchestration/pipeline.py`:

```
systemd timer (VPS, IST market-hours-aware schedule)
        ↓
orchestration/daily.py
        ↓
 1. re-auth (OAuth + 2FA)
 2. ingest → validate → staleness check           → write health_check.json after each stage
 3. generate signals (only if strategy has cleared the gate)
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

Every stage writes its own status to `health_check.json` incrementally. If the pipeline dies at step 4, the dashboard shows exactly that.

## Development

### Adding New Metrics

To add new metrics to the dashboard:

1. Add the metric to `health_check.json` (or the appropriate data file)
2. Update the corresponding screen function in `main_dashboard.py`
3. Test with sample data

### Customizing Screens

Each screen is a separate function in `main_dashboard.py`:
- `render_command_center()` - Screen 1
- `render_research_pipeline()` - Screen 2
- `render_risk_kill_switch()` - Screen 3
- `render_capital_ladder()` - Screen 4
- `render_reconciliation()` - Screen 5
- `render_alerts()` - Screen 6

### Styling

Custom CSS can be added in the `main()` function. Use Streamlit's native styling where possible.

## Testing

### Unit Tests

Run the dashboard with sample data to verify all screens work:

```bash
# Generate sample data
python scripts/generate_sample_data.py

# Run dashboard
streamlit run dashboard/main_dashboard.py
```

### Integration Tests

The dashboard should work with real data from:
- `observability/health.py` - HealthService
- `risk_kill/guard.py` - RiskGuard
- `reconciliation/engine.py` - ReconciliationEngine
- `execution/service.py` - ExecutionService

## Deployment

### Local Development
```bash
streamlit run dashboard/main_dashboard.py
```

### Production (VPS)
```bash
# Install dependencies
pip install streamlit pandas

# Run in background
nohup streamlit run dashboard/main_dashboard.py --server.port=8080 &

# Or with systemd
# See deploy/ for systemd service files
```

### Docker
```dockerfile
FROM python:3.11

WORKDIR /app
COPY . .

RUN pip install -r requirements.txt

CMD ["streamlit", "run", "dashboard/main_dashboard.py", "--server.port=8080", "--server.address=0.0.0.0"]
```

## Priority Order (from Section 6)

1. ✅ **Run the diagnostic protocol** - `python diagnostics/run_diagnostics.py`
2. ✅ **Log the diagnostic result** - Automatically saved as HYP-DIAG-001
3. If harness bug: Fix it, re-run all 20
4. If no edge after costs: Consider lower-turnover approaches or differentiated research
5. ✅ **Build the dashboard** - `streamlit run dashboard/main_dashboard.py`
6. **Screen 4's toggle** becomes interesting once strategies pass the gate

## Module Structure

```
dashboard/
├── __init__.py              # Module exports
├── main_dashboard.py        # Primary Streamlit dashboard (NEW - v1)
├── README.md                # This file
├── research_dashboard.py    # Legacy: Read-only research dashboard
├── paper_dashboard.py       # Legacy: Paper trading dashboard
├── broker_dashboard.py      # Legacy: Broker status dashboard
├── operational.py           # Operational status collection
├── server.py                # HTTP server for dashboards
├── cockpit_html.py          # HTML cockpit rendering
└── research_api.py           # Research API endpoints
```

## Related Modules

- `diagnostics/` - Diagnostic protocol (Section 0)
- `observability/` - Health monitoring and alerts
- `risk_kill/` - Kill switch implementation
- `orchestration/` - Daily pipeline
- `reconciliation/` - Position and order reconciliation
- `execution/` - Order execution service

## Strategy Dashboard (HTTP server, no Streamlit required)

**`dashboard/strategy_dashboard.py`** renders the one page that answers "is
anything working, and what do I do today":

- **Validated strategy card** — MomReM (momentum + market-regime filter):
  OOS Sharpe 0.97, CAGR 19.3%, MaxDD −16.3%, deflated Sharpe 0.999, robust
  to 3× costs. Every other researched family is shown on the leaderboard
  with an honest verdict (rejected / benchmark-like / validated).
- **Live signal** — recomputes MomReM's exact production logic (20-day
  cross-sectional momentum, top-20 equal weight, 100-day SMA regime filter
  on the equal-weight market proxy) from `data/clean/eod2_data`:
  current regime, strategy position (1-day execution lag), today's basket
  with share quantities for a given capital, next rebalance date, market
  breadth, and recent signal history.
- **Track record charts** — self-contained inline SVG equity curve,
  drawdown, and yearly returns vs benchmark (no external CDN/JS).
- **Data freshness** — surfaces stale data with the exact refresh commands,
  so a stale signal is never mistaken for a broken strategy.

### Routes (served by `dashboard/server.py`)

| Route | Content |
|---|---|
| `/` | Strategy dashboard (landing page) |
| `/cockpit` | Research cockpit (strategy experiments) |
| `/operations` | Read-only operational status |
| `/api/strategy/signal?capital=100000` | JSON signal payload |
| `/healthz` | Health check |

```bash
# Run the server
python dashboard/server.py            # or: make server

# Daily signal from the CLI
python scripts/daily_signal.py --capital 100000 --save
python scripts/daily_signal.py --capital 100000 --save --telegram  # needs TELEGRAM_BOT_TOKEN/CHAT_ID
```

Note: `dashboard/__init__.py` imports Streamlit lazily so the HTTP server
works in a minimal environment. The Streamlit main dashboard
(`main_dashboard.py`, all 6 screens) still runs via
`streamlit run dashboard/main_dashboard.py`.

## License

Part of the Quant India trading system. See repository root for license information.
