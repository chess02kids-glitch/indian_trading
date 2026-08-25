# Historical universe dataset

This directory holds the historical index-membership dataset used by research
backtests. It is the single source of truth for *which symbols were in which
index on which date*, and it is deliberately survivorship-bias-safe: removed /
delisted constituents are retained with a finite `valid_to` rather than
deleted.

## Schema

Every CSV (or Parquet) file has one row per constituent membership:

| column       | description                                                     |
| ------------ | --------------------------------------------------------------- |
| `symbol`     | Tradable ticker (uppercase, `.NS`-less research canonical)      |
| `index_name` | `nifty50`, `nifty100`, or `nifty500`                            |
| `valid_from` | First date the symbol was a member (`YYYY-MM-DD`)               |
| `valid_to`   | Last date the symbol was a member (`YYYY-MM-DD`; blank = current) |
| `isin`       | Optional ISIN                                                   |
| `sector`     | Optional sector label (best-effort research metadata)           |
| `exchange`   | Exchange (`NSE`)                                                |
| `delisted`   | Whether the name was removed from the exchange (survivorship)   |

## Provenance

Index membership changes at each NSE index review (typically June and
December). This repository ships a **curated research snapshot** (membership
valid from `2023-01-01` onward, current constituents plus a set of documented
former members with finite `valid_to`) so that the date-resolution and
survivorship machinery is exercised without a network fetch. Regenerate with:

```bash
python scripts/regenerate_universe.py
```

To refresh from an authoritative source, replace these rows from the NSE index
factsheet / bseindia constituent tables using the same schema; the loader
(`data.universe`) requires no code changes. Research experiments record the
dataset fingerprint, so a regeneration that changes membership will not
silently change historical backtest claims.

## `nifty100-pit/` (v0.7 real-data universe)

A **point-in-time** Nifty 100 membership dataset derived from
[`aditya-jha/nse-historical-membership`](https://github.com/aditya-jha/nse-historical-membership)
(data license **CC BY 4.0** — attribution: *underlying source: NSE index
press releases / NSE exchange circulars (publicly published)*). It contains
the full membership history (210 rows / 175 symbols) plus:

* `provenance.json` — source commit, SHA-256 of the source CSV, membership
  fingerprint, and per-row NSE press-release URLs;
* `panel_symbols.txt` — the deterministic research-panel symbol list
  (complete, gap-free price history in the window) used by the operator
  fundamentals command.

It lives in a subdirectory because `load_universe_dataset()` reads only
*direct* CSV children of this directory — the v0.6 snapshot universes above
are therefore untouched. The v0.7 real-data experiment
(`scripts/run_real_data_experiment.py`) loads it explicitly and records its
fingerprint in every run and ledger entry.
