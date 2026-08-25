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
