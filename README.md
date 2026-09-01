# Indian Trading

A research-to-paper-trading system for Indian equities: a shared data layer, a
cost-aware research engine, one validated strategy (MomReM), and a virtual
portfolio — all behind **one** dashboard.

> **Nothing here places a real order.** There is no code path from this
> repository to a broker order API. The Upstox integration is read-only quotes.
> Everything labelled *paper* is fake money on real prices.

**New here? Read [`docs/BEGINNER_GUIDE.md`](docs/BEGINNER_GUIDE.md) first.** It
explains what the system is, the 5-minute daily routine, how to read every
number, and the capital ladder. This file is only the install reference.

---

## Install

Requires **Python 3.11+**.

```bash
git clone <this repo> && cd indian_trading
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e .[dev]
```

If `pip install -e .[dev]` pulls in optional integrations you do not need
(`supabase`, `upstox-python-sdk`, `mlflow`, `yfinance`), the minimum that runs
every dashboard page is:

```bash
pip install "numpy>=1.26,<3.0" "pandas>=2.0" "pyarrow>=23.0.1" pytest
```

No internet connection is needed to run the system — the price history ships in
the repository. You need internet only to refresh data or install packages.

## Run

```bash
python dashboard/server.py
```

Then open **http://localhost:8080/** — one URL for the whole system.

```
Quant India unified dashboard: http://0.0.0.0:8080/
  ├─ strategy    http://0.0.0.0:8080/strategy
  ├─ live        http://0.0.0.0:8080/live
  ├─ paper       http://0.0.0.0:8080/paper
  ├─ research    http://0.0.0.0:8080/cockpit
  └─ operations  http://0.0.0.0:8080/operations
```

The first request takes 30–90 s while ~2.7 M price rows are read and cached;
after that navigation is instant. The legacy pages are still served at their own
routes, but `/` is the unified interface and you never need more than one tab.

## Daily use

```bash
python fetch_data.py        # 1. refresh end-of-day prices (after 15:45 IST)
python dashboard/server.py  # 2. open the dashboard, click "Recompute signal"
python scripts/daily_signal.py   # or print today's basket to the terminal
```

Full routine, with what each step means and how long it takes:
[`docs/BEGINNER_GUIDE.md`](docs/BEGINNER_GUIDE.md).

## Tests

```bash
pytest tests/
```

Some modules are skipped or error at collection when optional integrations are
absent (`supabase`, `yfinance`, `scipy`, `mlflow`, `upstox-python-sdk`,
`pytest-asyncio`). The core suite — data layer, strategies, paper trading, risk
guard, dashboards — runs without any of them:

```bash
pytest tests/test_datahub.py tests/test_local_paper_trading.py \
       tests/test_strategy_dashboard.py tests/test_risk_kill.py -q
```

`tests/test_datahub.py` is the fastest way to see the system's invariants
written as executable code, including regression tests for every defect fixed in
the unification work.

## Layout

| Path | What it is |
|---|---|
| `datahub/` | **Shared data layer.** Panel, universe, quotes, heartbeats, analytics. Every page reads through this so no two pages can disagree about the data. |
| `dashboard/server.py` | The HTTP server for the unified dashboard and all APIs. |
| `dashboard/app/` | The unified single-page front end (`index.html`, `app.css`, `app.js`). Dependency-free — no build step, no CDN. |
| `dashboard/strategy_dashboard.py` | MomReM signal computation and the standalone strategy page. |
| `dashboard/operations.py` | Real operations report: broker, token expiry, reconciliation, heartbeats, kill switch. |
| `paper_trading/` | The virtual account: ledger, quotes, rebalance engine. |
| `research_live/` | The research engine: strategies, metrics, simulation, the MomReM overlay. |
| `risk_kill/` | Deterministic risk guard. Stdlib only, fails closed, no LLM in the path. |
| `reconciliation/`, `broker/` | Ledger reconciliation and read-only broker status. |
| `data/eod2/daily/` | Raw NSE end-of-day mirror (~3,700 symbols). |
| `data/clean/eod2_data/` | Cleaned, split-adjusted parquet bundle. |
| `data/clean/prices.parquet` | Materialised on demand from the shared panel. |
| `var/` | Derived, gitignored: caches, the paper SQLite ledger, system state. |

## Expanding the universe

The clean bundle holds ~133 names; the raw mirror already in the repository holds
~3,700. Promote more of them with:

```bash
python scripts/expand_universe.py                       # research-grade defaults
python scripts/expand_universe.py --min-years 5 --min-value 3000000
python scripts/expand_universe.py --symbols ZOMATO,TRENT,POLICYBZR
python scripts/expand_universe.py --dry-run             # just list candidates
```

Or use **Data & universe → Add more stocks** in the dashboard. Then click
**Recompute signal**. The result is written to `var/cache/broad_universe.parquet`,
which is derived data — never committed, always rebuildable.

## Real quotes (optional)

Out of the box the intraday tape is simulated from verified end-of-day history
and is clearly labelled `SIM`. To use real quotes:

```bash
export UPSTOX_ACCESS_TOKEN="..."   # a daily OAuth access token, not the API key
python dashboard/server.py
```

The quote chip switches from `SIM` to `UPSTOX`. The system's inability to place
orders is unchanged, because it never had one. Tokens expire roughly daily; the
Operations page shows a countdown.

## Known issues

- The published MomReM card reports an out-of-sample Sharpe of 0.966. A fresh
  recomputation of the strategy *as specified* gives ~0.63. The published figure
  came from a rebalance-grid bug that never cleared names dropping out of the
  top-20, so the backtest held ~272 names instead of 20. The bug is fixed in
  source; **plan around the recomputed number.** See section 5.2 of the guide.
- Nine test modules fail to collect without `supabase`/`yfinance`, and
  `tests/test_live_feed.py` needs `pytest-asyncio`. Both pre-date this work.
