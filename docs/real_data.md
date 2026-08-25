# Real-Data Foundation (v0.7)

This document covers the v0.7 milestone: connecting **real Indian equity
data** to the frozen v0.6 research contracts, validating it, and re-running
the **exact same** v0.6 baseline (3M cross-sectional momentum × quality
screen, monthly, long-only, inverse-volatility weighted, 15% volatility
target) on real data. No strategy logic, parameters, schedule, cost
assumptions, or gate thresholds were changed by v0.7 — the data is the only
input that differs.

## Data sources

| role | source | pin | license / provenance |
| ---- | ------ | --- | -------------------- |
| primary prices | [`BennyThadikaran/eod2_data`](https://github.com/BennyThadikaran/eod2_data) (NSE official daily reports mirror) | commit recorded in `reports/generated/real_data/completeness_report.json` (`prices.source.commit`) | **no license file in the repository**; the data is a republished mirror of NSE's official public daily reports (www.nseindia.com/all-reports). Usage here is research-only and documented in this file; treat redistribution as disallowed unless NSE/upstream terms are confirmed |
| point-in-time universe | [`aditya-jha/nse-historical-membership`](https://github.com/aditya-jha/nse-historical-membership) (NSE index press releases) | commit recorded in `data/universe/nifty100-pit/provenance.json` | code MIT; **data CC BY 4.0** — attribution: *Point-in-time NSE index membership from github.com/aditya-jha/nse-historical-membership (data: CC BY 4.0); underlying source: NSE index press releases / NSE exchange circulars (publicly published)* |
| fundamentals (quality screen) | operator bundle fetched with `yfinance` (Yahoo Finance public endpoint) | `data/bundle/fundamentals_provenance.json` (yfinance version, per-symbol coverage, `bundle_fingerprint`) | read-only market-data access only; **no broker, no trading API, no credentials** |

The primary and validation roles are intentionally separated: prices come
from the eod2 mirror of NSE's own reports, while the operator bundle fetches
an **independent** yfinance close series used purely as a cross-check
(`data/bundle/crosscheck_yfinance.json`) — days where eod2's
split/bonus-adjusted close diverges >1.5% from yfinance's raw close are
candidate split/bonus dates for manual review (report only, never
auto-repaired).

## Adjustment / corporate actions

* eod2 `daily/` is **split/bonus-adjusted, dividends NOT adjusted** (upstream
  README). Every normalised row carries `adjustment_state`
  (`split_bonus_adjusted`) plus `source`, `exchange`, `ingested_at`
  (the source `meta.json lastUpdate` — deterministic, not the wall clock)
  and `source_ts`.
* The eod2 `meta.json` `equityActions` list covers **only the current
  upcoming window** — the source has no historical 2023–2026 action list.
  Split/bonus dates are therefore *detected* via the independent yfinance
  cross-check (operator bundle) and via the in-window OHLC consistency gate,
  and the residual uncertainty is recorded as a limitation, not assumed
  away.
* In the pinned snapshot, **zero** OHLC-inconsistent rows fall inside the
  research window (all 1,117 inconsistencies across 33 symbols pre-date
  2023; reported in the completeness report, never repaired).
* HDFC (member until 2023-07-13, delisted at the HDFC/HDFC Bank merger) has
  no price file in the mirror; it is excluded with an explicit reason while
  HDFCBANK (a distinct ISIN, member throughout) covers the bank exposure.

## Repository layout added in v0.7

```
ingestion/eod2_adapter.py            # thin eod2 normaliser (source -> canonical contract)
ingestion/nse_membership_adapter.py  # PIT membership normaliser (CC BY 4.0 source)
research/realdata.py                 # panel assembly, PIT mask, bundle loader, completeness
scripts/ingest_real_data.py          # 3 modes: --local / --fetch-fundamentals / --from-bundle
scripts/run_real_data_experiment.py  # frozen v0.6 baseline on real data (asserts no drift)
data/universe/nifty100-pit/          # committed PIT universe CSV + provenance + panel symbols
data/bundle/                         # operator artifacts (git-ignored, .gitkeep kept)
data/raw/eod2_data/NSE/...           # raw layer, window-scoped (git-ignored)
data/clean/eod2_data/*.parquet       # validated clean layer (git-ignored)
reports/generated/real_data/         # completeness + experiment reports (git-ignored)
```

The v0.6 synthetic pipeline (`scripts/run_research_experiment.py`,
`data/universe/*.csv` snapshots) is untouched. The PIT universe lives in its
own subdirectory because `load_universe_dataset()` only reads *direct* CSV
children of `data/universe/`.

## Pipeline (raw → validated → research)

1. `scripts/ingest_real_data.py --local --eod2-dir <eod2 checkout>
   --membership-dir <membership checkout> [--as-of ...]
   [--window-start ...]` — offline, deterministic:
   * membership CSV (CRLF upstream) → `data/universe/nifty100-pit/`
     (CSV + `provenance.json` with source commit, SHA-256 of the source
     CSV, membership fingerprint, per-row NSE press-release URLs);
   * eod2 `daily/*.csv` → canonical long frame → raw layer
     (`data/raw/eod2_data/NSE/<SYM>/<YYYY>/<MM>.parquet`, **window-scoped** —
     full history stays pinned at the source commit) → quality gate →
     clean layer (`data/clean/eod2_data/<SYM>.parquet` + `.meta.json` with
     row fingerprint);
   * rectangular close/high/low/volume panel over the **maximum clean
     overlapping period** (data-derived calendar — holidays and special
     sessions included); symbols with gaps are excluded *with an explicit
     reason* (never silently dropped);
   * §7 completeness report (`completeness_report.json/.md`).
2. **Operator, one external-data command** (network needed; Arena's egress
   is restricted to PyPI/GitHub, so this is the single command an operator
   runs on their own machine):

   ```bash
   python scripts/ingest_real_data.py --fetch-fundamentals
   ```

   produces `data/bundle/fundamentals_quarterly.parquet` (long
   `date=availability / symbol / roe / debt_to_equity`),
   `data/bundle/crosscheck_yfinance.json` and
   `data/bundle/fundamentals_provenance.json`. Availability rule: a
   fiscal quarter's figures are treated as knowable at the **next quarter
   end** (conservative vs NSE's ~45-day filing deadline — no publication
   look-ahead).
3. `scripts/ingest_real_data.py --from-bundle data/bundle` — offline merge
   into the research dataset + refreshed completeness report (no network).
4. On a fresh machine, reproduce the v0.6 entry first so the shared ledger
   is consistent, then run the real-data baseline:

   ```bash
   python scripts/run_research_experiment.py     # v0.6, reproduces HYP-00001
   python scripts/run_real_data_experiment.py    # v0.7, appends HYP-00002
   ```

   The real-data script **refuses** to allocate `HYP-00001` (it would mean
   overwriting the v0.6 entry) and exits with instructions instead.

## Point-in-time universe semantics

`data/universe/nifty100-pit/nifty100.csv` holds the full membership history
(210 rows / 175 symbols for Nifty 100, since 2014 in the source). The
research strategy receives a boolean `date × symbol`
`active_members` mask built from `valid_from`/`valid_to`; the cross-sectional
momentum and quality quantiles rank **only within each date's actual
members** (rank-then-mask would be a look-ahead/label bug — a regression
test pins the semantics). An incomplete mask can only be conservative
(unknown ⇒ not a member). Membership is stable after the 2026-03-30
reconstitution; the source's coverage date (2026-05-15) is recorded in the
universe provenance and verified against the upstream current snapshot.

## Reproducibility (v0.7 §14)

A real-data run is deterministic given: the eod2 source commit, the
membership source commit, the operator bundle (fingerprinted by
`bundle_fingerprint`), the committed PIT universe (fingerprinted by
`membership_fingerprint`), the code commit, and the frozen configuration
(asserted against the locked v0.6 literals before any engine run — drift
aborts). Every dataset input carries a hash/fingerprint in the report, the
experiment summary and the ledger; a future re-run on changed data produces
a different fingerprint and therefore a different `dataset_version`.
`ingested_at` uses the source data timestamp (not the wall clock) so that
re-ingestion of the same pinned source commit is byte-for-byte
reproducible (covered by the deterministic-snapshot test).

## Leakage-check status (v0.7 §13)

| check | status |
| ----- | ------ |
| future-date contamination | **verified** — `as_of` at ingestion + quality gate (`future_date` rows excluded, counted); tests 8/9 |
| constituent look-ahead | **verified** — PIT `valid_from`/`valid_to` + per-date mask + semantics regression test |
| corporate-action look-ahead | **partially verified** — adjusted series is source-asserted; in-window OHLC verified clean; historical 2023–2026 action list is not in the source, so split/bonus detection relies on the operator cross-check (see limitations) |
| feature leakage (momentum) | **verified** — trailing-only 63-day lookback |
| publication/availability timing (fundamentals) | **verified** — one-quarter conservative lag; availability dates after `as_of` are dropped and counted, never used |
| holdout contamination | **verified** — `run_holdout_protocol` window guards + untouched locked holdout |
| timestamp alignment | **verified** — data-derived calendar (902 trading days, 54 weekday holidays, special Sunday session 2026-02-01 with candles) |

## Security (v0.7 §24)

No broker execution, no order placement, no LIVE path, no OAuth trading
credentials. All market-data access is read-only. Configuration is
environment-only (`QUANT_DATA_DIR`); no API keys or credentials appear in
source, reports, or artifacts. Research storage is local Parquet/DuckDB —
no Supabase dependency. No license violation is knowingly introduced: the
CC BY 4.0 data source is attributed, and the no-license eod2 mirror is
flagged as a research-only usage with the caveat documented (above).

## Known limitations

1. **eod2_data has no license file** — research-only mirror of NSE official
   public daily reports; confirm terms before any wider use/redistribution.
2. **Historical corporate actions (2023–2026) are not enumerated in the
   source** — the `equityActions` list covers only the current upcoming
   window; split/bonus detection for the research window depends on the
   operator's independent yfinance cross-check (review
   `crosscheck_yfinance.json` mismatches).
3. **HDFC** (delisted 2023-07-13 at the merger) has no price file in the
   mirror — excluded with reason; HDFCBANK covers the exposure.
4. **Seven post-window-start IPOs** (JIOFIN, BAJAJHFL, HYUNDAI, SWIGGY,
   ENRIN, TATACAP, TMCV) lack pre-listing prices inside the window — they
   are PIT members but not panel symbols (excluded with reason).
5. **Fundamentals availability is coarser than reality** (conservative
   next-quarter-end rule): no publication look-ahead, but information
   becomes effective later than it truly does.
6. **The 126-symbol panel is the complete-history subset** of the 134
   constituents overlapping the window; excluding gap symbols makes the
   panel slightly survivorship-favourable (only names with *complete*
   history can be selected).
7. **Synthetic v0.6 results are a framework test only** — the synthetic and
   real results are NOT comparable performance estimates.
