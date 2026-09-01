# The Complete Beginner's Guide to This System

**Read this once, end to end. It takes about 35 minutes and it is the highest-return
35 minutes you will spend here.** Everything after this is operating the machine.

This guide assumes you know what a stock is and have a broker account. It assumes
nothing else — no Python, no statistics, no trading system experience.

---

## 1. What you actually have

You have a **research-to-paper-trading system for Indian equities**. It is not a
money-printing machine and it is not connected to your broker. Concretely, it is
four things bolted together:

1. **A data layer.** ~3,700 daily NSE price files (`data/eod2/daily`) plus a
   cleaned, split-adjusted parquet bundle (`data/clean/eod2_data`). Everything
   reads through one module, `datahub`, so no page can disagree with another
   about what data exists.
2. **A research engine.** It backtests strategy ideas with real Indian costs
   (brokerage, STT, exchange fees, slippage), splits history into
   in-sample/out-of-sample, and runs a gate that rejects most ideas.
3. **One validated strategy — "MomReM".** Cross-sectional momentum (buy the
   top-20 names by trailing 20-day return) with a market-regime filter (go to
   cash when the equal-weight market proxy falls below its 100-day moving
   average). Rebalanced every 20 trading days.
4. **A virtual ("paper") account.** Fake rupees, real prices, a real ledger in
   `var/paper_trading.sqlite`. **There is no code path from this system to a
   broker order API.** That is deliberate and it is enforced by tests.

### The single most important thing to understand

> **Nothing in here places a real order. "Paper" means the money is imaginary.**
> The Upstox integration is *read-only quotes*. If you delete every credential
> from this repository, the worst thing that can happen is that the quote tape
> falls back to clearly-labelled simulated prices.

That safety property is why you can leave it running, click things, and break
things without financial consequence. Use that freedom to learn.

---

## 2. Install and run (about 10 minutes, once)

### 2.1 Prerequisites

- Python **3.11 or newer** (`python3 --version`)
- ~2 GB of free disk (the repository already carries the price history)
- No internet connection is needed to *run* the system. You need internet only
  to refresh data or install packages.

### 2.2 Install

```bash
cd indian_trading
python3 -m venv .venv                 # create an isolated environment
source .venv/bin/activate             # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install "numpy>=1.26,<3.0" "pandas>=2.0" "pyarrow>=23.0.1" pytest
```

That is the minimum to run every page in the dashboard. You do **not** need
`upstox-python-sdk`, `supabase`, or `mlflow` to start.

### 2.3 Start the dashboard

```bash
python dashboard/server.py
```

You should see:

```
Quant India unified dashboard: http://0.0.0.0:8080/
  ├─ strategy    http://0.0.0.0:8080/strategy
  ├─ live        http://0.0.0.0:8080/live
  ├─ paper       http://0.0.0.0:8080/paper
  ├─ research    http://0.0.0.0:8080/cockpit
  └─ operations  http://0.0.0.0:8080/operations
```

Open **http://localhost:8080/** — that one URL is the whole system. Everything
else in the sidebar is a section of the same page, served by the same process,
reading the same data.

> **First load is slow (30–90 s).** It reads ~2.7 million price rows and caches
> them. Later navigations are instant. If you want the wait to be worth more,
> expand the universe first (section 7).

---

## 3. The one-page tour

The sidebar has eleven sections in four groups. Here is what each is for and
how much attention it deserves.

| Section | What it answers | Check it |
|---|---|---|
| **Overview** | "Is anything wrong, and what do I do today?" | Every day, first |
| **Strategy** | "What should I hold right now?" | Every day |
| **Divergence** | "Is live behaviour matching the backtest?" | Every day, 10 seconds |
| **Risk & sizing** | "How much should I actually buy?" | Before any trade |
| **Correlation** | "Are my strategies the same bet twice?" | Before adding a strategy |
| **Live terminal** | Intraday charts + a demo AI trader | When you want to watch |
| **Paper account** | The virtual portfolio itself | Weekly |
| **Research cockpit** | Test a new idea | Occasionally |
| **Data & universe** | What data exists; add more stocks | Monthly |
| **Operations** | Is every component alive? | When something looks wrong |
| **Beginner guide** | This document | Once, then when stuck |

### The status strip (top right, always visible)

Four chips that summarise the whole system at a glance:

- **data** — the date of the newest price bar and how many days old it is.
  Green = ≤5 days. Amber = stale. **This is the #1 cause of a "broken-looking"
  dashboard.**
- **feed** — `SIM` (simulated intraday ticks from verified EOD history) or
  `LIVE` (real Upstox quotes). Hover for the explanation.
- **quotes** — which source priced the paper watchlist: `UPSTOX`, `SIM`, or `EOD`.
  **`SIM` is not an error.** It means no Upstox token is configured, so the
  system is running on clearly-labelled simulated prices instead of failing.
- **regime** — `IN MARKET` or `IN CASH`. This is the strategy's own filter.

### The kill switch (bottom left, always visible)

A real, persisted flag. When armed it blocks every paper rebalance, disables
automatic paper trading, and stops the demo bot. It exists so you can freeze the
system instantly when something looks wrong. Practice arming and disarming it —
it costs nothing and one day it will be the first thing you reach for.

---

## 4. Your daily routine

**Total time: 5 minutes, once a day, after the market closes (after 15:45 IST).**

Do not do this intraday. The strategy is a daily/monthly-rebalance strategy;
watching it tick by tick will teach you nothing and cost you sleep.

### Step 1 — Refresh the data (2 minutes)

```bash
python fetch_data.py
```

Then in the dashboard: **Overview → Recompute signal**.

The `data` chip should turn green and show today's date. If it stays amber, the
fetch failed — check your internet connection and re-run it. **Never act on a
signal computed from stale data.** The Strategy page will show a red banner if
you try.

### Step 2 — Read the regime (20 seconds)

Overview shows one pill: **IN MARKET** or **IN CASH**.

- **IN MARKET** — the equal-weight market proxy is above its 100-day SMA. The
  strategy holds its basket.
- **IN CASH** — the proxy is below the SMA. The strategy holds nothing.

That is the entire risk management of this strategy. It is crude, it is
deliberate, and it is what turns a −58% benchmark drawdown into a −16% one.

### Step 3 — Check divergence (30 seconds)

Look at the **z-score** on the Overview card (details on the Divergence page).

| z-score | Meaning | Action |
|---|---|---|
| \|z\| < 1 | Live is inside normal noise vs the backtest | Nothing |
| 1 ≤ \|z\| < 2 | Drifting. Could be noise, could be early trouble | Start logging it daily |
| \|z\| ≥ 2 | Live is 2σ from what the backtest implies | **Stop adding capital.** Investigate |

When z ≥ 2, investigate **in this order** — do not skip steps:

1. Is the quote source `SIM` when you thought it was real?
2. Is the data actually fresh (the `data` chip)?
3. Did fills happen at prices you assumed?
4. Only then: is the market in a regime the backtest never saw?

Nine times out of ten the answer is 1, 2, or 3.

### Step 4 — Look at the basket, act deliberately (2 minutes)

Strategy → **Today's basket** gives you the top-20 names with quantities for
your capital. Two quantity columns are shown:

- **Qty (equal)** — 1/20th of capital in each name. This is what the backtest
  assumed.
- **Qty (vol-tgt)** — sized inversely to each name's realised volatility, so a
  40%-vol smallcap gets fewer rupees than a 15%-vol largecap. This is the more
  sensible default for real money.

Then **stop**. Write the basket down or export it. Place orders yourself, at
your broker, when you decide to. The system will never do it for you.

### What you should NOT do daily

- Do not rebalance more than once every 20 trading days. Turnover is the enemy:
  the strategy turns over ~25× a year, and every extra rebalance hands money to
  costs.
- Do not "improve" the basket by swapping in a stock you like. That is not the
  strategy any more, and none of the validation applies to it.
- Do not override the regime filter. "I think the market will go up" is exactly
  the judgement the filter exists to replace.

---

## 5. How to read the numbers (and not be fooled by them)

### 5.1 Sharpe ratio

Return above the risk-free rate, divided by volatility. Rules of thumb for a
daily-rebalanced long-only equity strategy:

- < 0.3 — not worth your time
- 0.3–0.7 — ordinary; a Nifty index fund does about this
- 0.7–1.2 — good
- \> 1.5 — **be suspicious**. Either it is genuinely excellent or something is
  wrong with the test.

### 5.2 The two columns you must never confuse

The Strategy page has a table titled **"Published card vs a fresh
recomputation"**. Read the note above it before you read any number.

| | Published card | Recomputed now |
|---|---|---|
| Source | `research_live/deliverables/STRATEGY_REPORT.md` | `datahub.analytics`, run live |
| OOS Sharpe | 0.966 | ~0.63 |
| Names held | ~272 (bug) | 20 (as specified) |

**What happened.** The research code that produced the published card built its
target portfolio by writing the top-20 names into an empty table and then
forward-filling it. It never *cleared* the names that dropped out of the top-20.
Forward-fill therefore carried every historically-selected name forward forever,
so the backtest held ~272 names on average instead of 20 — economically much
closer to "equal-weight the whole liquid universe with a regime filter" than to
"a top-20 momentum book".

That bug is now fixed in `research_live/mom_overlay.py` and
`research_live/strategies.py`. The recomputed column is the strategy implemented
exactly as its own card describes it.

**Which number should you plan around? The recomputed one.** And even then,
treat it as an estimate with wide error bars, not a promise.

### 5.3 Max drawdown

The largest peak-to-trough fall. The recomputed strategy's OOS max drawdown is
roughly **−33%**. Ask yourself honestly: *if my ₹5,00,000 became ₹3,35,000 and
stayed there for eight months, would I still be running this?* If the answer is
no, your position is too big. That is what the Risk & sizing page is for.

### 5.4 Cost sensitivity — the most useful panel in the system

The slider on **Risk & sizing** re-runs the real backtest at one-way costs from
2.5 to 40 basis points. Watch the Sharpe fall as you slide right.

One-way cost means what you pay on *each side* of a trade:

| Cost | What it represents |
|---|---|
| 5 bps | Best case: discount broker, limit orders, large caps only |
| 15 bps | The research assumption: brokerage + STT + exchange + modest slippage |
| 30 bps | Realistic for mid/small caps, market orders, or a bad day |

If your Sharpe is already near zero at 15 bps, **the edge is fragile** and a
small change in execution destroys it. That is worth knowing before you fund an
account, not after.

### 5.5 Kelly and risk of ruin

**Kelly** tells you the mathematically growth-maximising fraction of capital to
risk. Full Kelly assumes you know your true win rate and payoff ratio. You
estimated them from ~198 trades, so you don't. **Use the quarter-Kelly number the
page shows** — it is the sane starting point.

**Risk of ruin** bootstraps the strategy's own historical daily returns into
thousands of possible futures and counts how often equity falls more than 35%
from a peak within three years. If that number is above ~20%, you are sizing too
aggressively for the strategy's actual tail behaviour.

### 5.6 Correlation

The heatmap on the **Correlation** page shows how similarly candidate strategies
move. Above **0.8** average pairwise correlation, running three strategies is
not diversification — it is the same bet three times with three times the
operational overhead. The verdict line says this in plain words.

---

## 6. The four things that were broken, and what they are now

If you read an earlier report about this system, it listed four defects. Here is
what each one was and where the fix lives.

**1. Pages disagreed about whether data existed.** The Research Cockpit looked
for `data/clean/prices.parquet`, which nothing ever wrote. The Strategy
Dashboard read `data/clean/eod2_data` directly. The Live Terminal read raw CSVs.
Now all three read `datahub`, and `prices.parquet` is *materialised from that
same panel*, so the Cockpit's default path always exists and always matches.

**2. The quote feed was permanently in error.** With no Upstox token, every
refresh raised, the ledger recorded a permanent error, and the five default
watchlist symbols never got a price. Now quotes come from a labelled chain:
`UPSTOX → SIM → EOD`. A missing token degrades to `SIM` (clearly marked as not
real), and `ERROR` is reserved for genuine failures. Indices like `NIFTY_50` are
anchored from the raw EOD mirror.

**3. The Strategy Dashboard could not compute its own signal.** Universe
selection required a symbol to have a bar on the single newest date in the whole
panel. The bundle legitimately contains more than one "last bar" date, so the
intersection was empty and the signal died with *"no data for signal
computation"*. Selection now uses a rolling recency window **and reports exactly
what it rejected and why** — see the Universe audit table on the Strategy page.

**4. Operations was a placeholder of `unknown` values.** It read a status file
nothing wrote. Now it is built from components that exist: token expiry
countdown, last successful quote, ledger audit plus expected-vs-actual position
count from the strategy signal, a functioning kill switch, and heartbeats every
component writes when it succeeds. A component that has never run says
**`never`** — never "healthy".

---

## 7. Adding more stocks

The clean bundle holds ~133 names. The raw NSE mirror **already in this
repository** holds ~3,700. Promoting more of them widens the universe the
strategy can choose from.

### From the dashboard

**Data & universe → Add more stocks.** Set the filters and click
**Build universe**. It takes 30–60 seconds and writes
`var/cache/broad_universe.parquet` (derived data, gitignored, rebuildable).

### From the command line

```bash
# the research universe: >=8 years of history, >= Rs 1 crore median daily value
python scripts/expand_universe.py

# bigger and looser
python scripts/expand_universe.py --min-years 5 --min-value 3000000

# specific names
python scripts/expand_universe.py --symbols ZOMATO,TRENT,POLICYBZR

# see what is available without building anything
python scripts/expand_universe.py --dry-run
```

### Which filters matter

| Filter | Default | Effect of loosening |
|---|---|---|
| Min history | 8 years | More names, but less reliable momentum estimates |
| Min median traded value | ₹1 crore/day | More names, but worse real-world fills |
| Recency | traded in the last 10 panel days | Fixed. Never loosen this. |

**Do not chase a big universe number.** The liquidity filter is what keeps the
backtest honest: a stock you cannot actually buy at the close is not a position,
it is a fiction. If you loosen it, the divergence tracker will eventually tell
you — that is one of the things it is for.

After expanding, click **Recompute signal** on the Overview page.

---

## 8. Getting real quotes (optional)

Out of the box the system runs on simulated intraday prices anchored to real
end-of-day closes. That is enough to learn the whole workflow. When you want
real quotes:

1. Create an app on the Upstox developer console.
2. Complete the OAuth flow to obtain a **daily access token**. Your API
   key/secret alone cannot fetch quotes.
3. Put the token in the environment before starting the server:

```bash
export UPSTOX_ACCESS_TOKEN="..."
python dashboard/server.py
```

The `quotes` chip will switch from `SIM` to `UPSTOX`, and the `feed` chip from
`SIM` to `LIVE`. **Nothing about the system's ability to place orders changes**,
because it never had one.

Upstox access tokens expire roughly daily. The Operations page shows a countdown
and flips to `TOKEN_EXPIRED` when it runs out. Re-authenticate; do not paste
tokens into the browser.

---

## 9. When something looks wrong

Work down this list in order. It resolves almost everything.

| Symptom | Cause | Fix |
|---|---|---|
| `data` chip amber/red | Price data is stale | `python fetch_data.py`, then Recompute signal |
| Basket is empty but regime is IN MARKET | Universe shrank to zero | Data & universe → check the rejection table → expand the universe |
| "Signal unavailable" | Panel could not load | Operations → System health → check the heartbeats |
| Quotes show `SIM` | No Upstox token | Expected. See section 8 if you want real quotes |
| Quotes show `ERROR` | A real failure | Operations → Broker health → read the detail line |
| Divergence z ≥ 2 | Live ≠ backtest | Section 4, step 3 — check source, freshness, fills, *then* regime |
| Reconciliation `DIVERGED` | Positions ≠ signal target | Run a paper rebalance, or accept the drift knowingly |
| Reconciliation `LEDGER_MISMATCH` | Cash doesn't reconcile | Export the CSVs from Operations and inspect |
| Everything is slow on first load | Cold cache | Normal. 30–90 s once, then instant |
| Page shows a red error box | An API payload failed | The box names the exception; check Operations for the component |

### The nuclear option

```bash
rm -rf var/cache var/system_state.json      # derived data + heartbeats
python scripts/expand_universe.py           # rebuild the universe cache
python dashboard/server.py
```

This never touches your price data or the research deliverables. To reset the
paper account itself, use **Paper account → Reset paper account** (it requires
you to type `RESET PAPER`).

---

## 10. The capital ladder — how to move toward real money

Do not skip rungs. Each one exists to answer a question the previous one cannot.

**Rung 0 — Read (0 rupees, 1–2 weeks).**
Follow the daily routine in section 4 without trading anything. Write down what
the basket *would* have told you to do. You are calibrating your expectations.

**Rung 1 — Paper trade (0 rupees, minimum 3 months / ~60 trading days).**
Paper account → set capital → **Start monitor**. Leave the dashboard running.
The divergence tracker needs real elapsed time; you cannot shortcut it.

*Gate to pass:* at least 40 trading days of equity history, a divergence z-score
that has stayed inside ±2, and reconciliation consistently `MATCHED` or `FLAT`.

**Rung 2 — Tiny real money (₹10,000–25,000, minimum 3 months).**
You place the orders yourself. The point is not profit; it is discovering your
own execution error — the gap between the price the model assumed and the price
you actually got. Log every fill.

*Gate to pass:* your realised execution slippage is under the cost assumption
the backtest used. If it isn't, go back to the cost-sensitivity slider and find
out whether the strategy survives your real costs.

**Rung 3 — Small real money (₹1–2 lakh, minimum 6 months).**
Only if rung 2 passed. Keep the position size at or below the quarter-Kelly
number on the Risk page.

**Rung 4 — Real money.**
Only if rung 3 passed and the divergence tracker has stayed calm through at
least one 10%+ market drawdown.

> **Most people should stop at rung 1 or 2.** A Nifty index fund beats most
> retail active trading after costs. This system's honest claim is a
> *risk-adjusted* improvement (better Calmar, much smaller drawdowns), not
> spectacular returns. Decide whether that is worth the effort before you fund
> anything.

---

## 11. What this system will never do

Stated plainly, because these are load-bearing properties, not marketing:

- It will never place, amend, or cancel a real order. There is no code path.
- It will never present a simulated price as a real one. Every quote carries
  its source, and the chips say `SIM` out loud.
- It will never invent a price for a position it cannot mark. Missing marks are
  reported, not fabricated.
- It will never report a component as healthy when it has never run. That is
  what `never` means in the heartbeat table.
- It will never let the AI/LLM layer influence the risk guard. `risk_kill` is
  deterministic, imports nothing outside the standard library, and fails closed
  on unknown inputs.

---

## 12. Where to go next

- `docs/RESEARCH_REPORT_2026-08-30.md` — the full research log, including every
  family that was tried and rejected.
- `research_live/deliverables/STRATEGY_REPORT.md` — the published MomReM card
  (read it alongside section 5.2 of this guide).
- `docs/risk.md` — the risk framework and the deterministic guard.
- `docs/local_paper_trading.md` — the paper account's data model.
- `docs/GO_LIVE_CHECKLIST.md` — the pre-flight list before any real capital.
- `tests/` — ~80 test files. `pytest tests/test_datahub.py` is a good place to
  start if you want to see the invariants written as code.

### The three habits that matter

1. **Refresh data, then recompute, then look.** In that order, every day.
2. **Trust the divergence tracker over your feelings.** It is the only thing in
   here that compares what you believe to what is happening.
3. **Size for the drawdown, not the return.** You will experience the −33%.
   Whether you survive it is a sizing decision you make today.
