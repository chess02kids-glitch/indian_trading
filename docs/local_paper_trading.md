# Local Paper Trading Dashboard

## What this is

The local Paper Trading page is a **virtual INR account** backed by a
read-only Upstox quote feed. It is designed for a trustworthy forward-paper
test after a strategy has passed the research gate.

- **Live market data:** Upstox full quotes (LTP, best bid/ask, volume), sampled
  every 30 seconds.
- **Virtual account:** cash, positions, virtual orders, marks, charges and P&L
  are stored only in `var/paper_trading.sqlite`.
- **No real execution:** `paper_trading/` contains no broker order, funds,
  holdings or GTT calls. A paper rebalance creates only local virtual fills.
  It cannot place an Upstox order.
- **Scope:** NSE cash-equity virtual positions and an intraday quote tape.
  Current options data can be added as a monitor later, but it is not a
  historical options backtester.

## Modes

| Dashboard source | Environment variable | Purpose |
| --- | --- | --- |
| **Upstox data only** (default) | `UPSTOX_ACCESS_TOKEN` | Current production-market quotes for marking the virtual portfolio. No orders can be sent. |
| **Upstox Sandbox** | `UPSTOX_SANDBOX_ACCESS_TOKEN` | Uses the Upstox SDK sandbox configuration and its quote access, if enabled for the account. Virtual fills still remain local. |

An **app API key and secret are not market-data credentials by themselves**.
Complete the Upstox OAuth login locally to obtain the short-lived access token.
Upstox access tokens typically expire daily, so the dashboard makes an expired
or absent token explicit instead of silently inventing prices.

Never put an API key, API secret, access token, or redirect URL in a source file
or chat. Store them only in a local, ignored environment file or your shell's
secret manager.

## One-time local setup

```bash
# From the repository root. Use your preferred virtual environment.
python -m venv .venv
source .venv/bin/activate
pip install -e '.[paper,dev]'

# Put the token in your local ignored .env / shell secret manager; do not commit it.
export UPSTOX_ACCESS_TOKEN='...'
export QUANT_EXECUTION_MODE=PAPER
export QUANT_PAPER_QUOTE_REFRESH_SECONDS=30

python dashboard/server.py
```

Open:

- `http://localhost:8080/` or `/paper` — virtual paper account, quote tape and P&L
- `http://localhost:8080/cockpit` — research/backtest cockpit
- `http://localhost:8080/strategy` — existing strategy-signal dashboard

Choose **₹10,00,000** (the default), another virtual amount, and the desired
source on the Paper page. Changing virtual capital is allowed only when there
are no open virtual positions; use the typed `RESET PAPER` confirmation to
start a new virtual account. This cannot change real broker funds.

## Forward-paper workflow

1. You provide the strategy rule, intended universe, frequency, position
   sizing, exits, and risk constraints.
2. We implement it as reviewed strategy code and test it against the relevant
   historical data. The backtest must include a one-bar delay, Indian charges,
   conservative slippage, benchmarks, out-of-sample validation and liquidity
   limits.
3. The strategy remains `RESEARCH_ONLY` until it passes the agreed research
   gate. `config/paper_strategies.json` records that decision.
4. A `paper_approved: true` strategy gets a target builder. The dashboard can
   then show a bid/ask-priced virtual order preview.
5. The user must type `PAPER REBALANCE` before virtual fills are recorded.
   The order ledger, realised/unrealised P&L and equity history then update from
   read-only quote marks.

The bundled MomReM entry intentionally starts as `RESEARCH_ONLY`; it cannot
rebalance the virtual portfolio until it is independently re-run and approved
on the selected dataset/cost assumptions.

## Realism and limitations

The paper account improves on a close-to-close backtest because virtual fills
use the observed best **ask** for buys and **bid** for sells, then add the
configured Indian cash-equity charges and base slippage allowance. It still
cannot guarantee a real fill: it does not yet model queue priority, depth beyond
the displayed quote, partial fills, exchange outages, market impact, or borrow.

The intraday history begins only while the local monitor is running. It stores
30-second quote snapshots for the watchlist and all virtual positions. That is
useful for building an intraday dataset going forward, but it is not a substitute
for multi-year tick/order-book history.

For this reason the page is appropriate for supervised paper validation, **not
for automatic real-capital deployment**.
