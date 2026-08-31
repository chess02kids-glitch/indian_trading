# Handoff: bring the Live Terminal onto real Upstox data

**Context for the handing-off agent (skip when copying the prompt below):**
everything buildable offline is already built and tested in this repository:

- `dashboard/live/feed.py` — the live-feed engine. Runs in **SIM mode** by
  default (random walk seeded from verified EOD history, clearly labelled).
  When an Upstox access token is configured it switches to **LIVE mode**:
  real v2 full quotes (10 s poll) drive prices and 1-minute bars, real v3
  1-minute + daily candle history is spliced in on warm-up, today's
  authoritative 1-minute bars are re-fetched on a rotating 90 s cadence,
  and the NSE market status is surfaced to the UI.
- `dashboard/live/upstox_source.py` — the **single file** that talks to
  Upstox. Read-only by construction (quotes, candle history, market
  timings only — no order/funds/GTT endpoints exist in it).
- `dashboard/live/web/live_terminal.{html,css,js}` — the UI. Green
  `LIVE · UPSTOX` badge vs amber `SIM FEED` badge, market status,
  8 timeframes, indicators, AI paper positions painted on the chart.
- `tests/test_live_feed.py` — 19 hermetic tests (SIM behaviour + LIVE
  wiring against a fake SDK). No network, no token.
- The whole LIVE path is proven end-to-end against a deterministic fake
  `upstox_client` SDK (mode flips to LIVE, prices follow quotes, candles
  splice cleanly, fallback works).

**What this agent (you, the one with internet + the token) must do:**
install the real SDK, set the real token, run the server, and verify the
dashboard shows **real** NSE prices. Copy the prompt below.

---

## THE PROMPT (copy from here)

```
You are completing the "Live Terminal" of the Quant India trading repo
(checkout: the working directory of this chat). Your job is to switch the
dashboard at /live from its clearly-labelled SIM feed to REAL Upstox
market data, verify it, and report. Read-only market data ONLY — never
place, modify or cancel orders, and never expose the token.

## Inputs you have
- Internet access (pip, HTTPS to api.upstox.com).
- A valid Upstox access token (daily OAuth token, e.g. starting with
  "eyJ..."). It is provided by the user as UPSTOX_ACCESS_TOKEN.
  (For a sandbox account use UPSTOX_SANDBOX_ACCESS_TOKEN instead.)
- Python 3.11+.

## Background (verified, do not re-derive)
- The dashboard server is `python dashboard/server.py` (default port 8080;
  override with the PORT env var). Routes: `/live` (UI),
  `/api/live/state`, `/api/live/candles?symbol=<SYM>&interval=<IVL>`,
  `/api/live/stream` (SSE).
- When UPSTOX_ACCESS_TOKEN is set in the server's environment and
  `upstox-python-sdk` is importable, the feed automatically builds a real
  Upstox source (dashboard/live/upstox_source.py), warms it in a background
  thread (per-symbol 1-minute history ~5 sessions + daily history ~400
  days, 0.25 s spacing), then the engine polls full quotes every 10 s and
  serves LIVE mode. The UI badge turns green "LIVE · UPSTOX" and every API
  response is stamped feed.mode = "LIVE".
- Instrument keys are resolved locally from
  data/universe/nifty500-pit/nifty500.csv (symbol -> NSE_EQ|<ISIN>);
  NIFTY_50 uses NSE_INDEX|Nifty 50. No key lookups hit the network.
- API shapes used (see .agents/skills/upstox/references/market-data.md):
  * HistoryV3Api.get_historical_candle_data(key, unit, interval, to_date,
    from_date) with unit in {minutes, days} -> resp.data.candles, rows are
    [ts, open, high, low, close, volume, oi] NEWEST-FIRST.
  * HistoryV3Api.get_intra_day_candle_data(key, "minutes", "1") -> today's
    1-minute bars.
  * MarketQuoteApi.get_full_market_quote(symbol="key1,key2,...",
    api_version="2.0") -> resp.data is {instrument_key: quote}; quote has
    last_price, volume, ohlc{open,high,low,close} (v2: ohlc.close is the
    PREVIOUS day close), depth{buy[0],sell[0]}.
  * MarketHolidaysAndTimingsApi.get_market_status(exchange="NSE")
    -> resp.data.status in {OPEN, CLOSED, PRE_OPEN, ...}.

## Steps
1. Install the SDK:
       pip install upstox-python-sdk
   (it is already declared in pyproject.toml; if the env uses a lockfile,
   `pip install -e .` also works).
2. Export the token in the SAME shell that runs the server:
       export UPSTOX_ACCESS_TOKEN="<the token>"
3. Start the server:
       python dashboard/server.py
   Warm-up takes roughly 30-60 s (22 symbols x 2 history calls, spaced).
4. Verify the feed is LIVE:
       curl -s localhost:8080/api/live/state | python3 -m json.tool | \
         grep -E '"mode"|healthy|warmed|market_status|error'
   Acceptance: feed.mode == "LIVE", feed.upstox.healthy == true,
   feed.upstox.warmed == true, feed.upstox.real_symbols == 23
   (22 watchlist equities + NIFTY_50), feed.upstox.error == null.
5. Verify the data is REAL (cross-check at least 3 symbols against the
   Upstox app or the Upstox website during market hours):
       curl -s 'localhost:8080/api/live/candles?symbol=RELIANCE&interval=1d&limit=3'
   The last daily close must match the symbol's real previous close to the
   tick. During the NSE session (09:15-15:30 IST, Mon-Fri) also check:
       curl -s 'localhost:8080/api/live/candles?symbol=TCS&interval=1m&limit=5'
   - bars are within the current minute's range,
   - volumes > 0,
   - /api/live/state universe "last" prices move over a 20 s sample.
6. Verify the UI: open http://localhost:8080/live — expect the green
   "LIVE · UPSTOX" badge with the NSE market status, watchlist prices
   matching your Upstox app, and the AI paper bot trading against those
   real prices (positions/P&L on the chart and in /paper).
7. If the market is CLOSED when you verify: steps 4-5 daily checks still
   apply (last real session's data), step 6 prices will be static until
   09:15 IST — that is correct, note it in the report.

## Hard constraints
- READ-ONLY: you may only add/modify code under dashboard/live/ if
  verification fails, and market-data fixes belong in
  dashboard/live/upstox_source.py (parsing, rate limits, error mapping).
  Do NOT add any order-placement, funds, portfolio or GTT API calls.
- Do NOT commit or echo the token. If a command would print it, mask it.
  (The server already keeps it in-process; it never appears in API
  responses — feed.upstox only reports configured/healthy/status fields.)
- Do NOT change the paper-trading ledger schema, the bot strategy, or the
  SIM feed. SIM must remain the labelled fallback when the token is absent
  or the real feed fails (3 consecutive failed quote polls auto-drop the
  feed to "SIM fallback" — that behaviour is a feature).
- If you hit HTTP 429 (rate limit) repeatedly during warm-up, raise
  UpstoxLiveSource.WARMUP_REQUEST_GAP_SECONDS (e.g. 0.25 -> 0.5) or
  QUOTE_POLL_SECONDS (10 -> 15) and retry; report the value you used.

## Failure triage (feed.upstox.error / detail strings)
- "no Upstox access token configured" -> token env var not visible to the
  server process (export it in the same shell, then restart).
- "upstox-python-sdk is not installed" -> pip install (step 1).
- 401/invalid token/refresh required -> the daily OAuth token expired
  (they are valid for one trading day): the user must re-authorize and
  hand over a fresh token; then restart the server.
- "no verified instrument-key mapping available" -> the
  data/universe/nifty500-pit/nifty500.csv file is missing/corrupt in this
  checkout; report it, do not invent keys.
- All symbols in sim_fallback_symbols with network errors -> outbound
  HTTPS to api.upstox.com is blocked; report the exact error text.

## Report back
- Exact feed.mode + feed.upstox block (pretty-printed, token never shown).
- The 3-symbol cross-check table (dashboard value vs Upstox app value).
- UI screenshot description: badge colour/label, market status line.
- Any code change you made (file, why) — expected to be none or tiny.
- One line: PASS (LIVE with real data verified) or BLOCKED (<reason>).
```

---

## Notes for the user (not part of the prompt)

- The token is a **daily OAuth token**: it expires every day. Re-authorize
  in the Upstox developer console (your app's "Authorize" flow) and
  restart the server with the new value. A sandbox token
  (`UPSTOX_SANDBOX_ACCESS_TOKEN`) works for the same code path but is only
  valid for sandbox-app instruments.
- Nothing in the repo stores the token; it lives only in the environment
  of the server process.
- Until the token is set, the dashboard stays on the honest SIM feed
  (amber badge) — anchored to the same verified EOD history, so levels
  are realistic even in SIM.
