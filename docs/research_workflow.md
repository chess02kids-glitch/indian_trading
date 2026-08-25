# Production research workflow

The v0.3 research workflow turns a validated backtest into a scientific
record before any paper capital is allocated:

```
Historical Data
      ↓
Factor Generation
      ↓
Strategy
      ↓
VectorBT Backtest
      ↓
Statistical Validation (DSR, purge/WF, CPCV, bootstrap CIs)
      ↓
Research Gate (PASS/FAIL/FRAGILE/INSUFFICIENT_EVIDENCE)
      ↓
Factor Diagnostics  →  Research Reports  →  MLflow (metadata-only)
      ↓
Paper Trading Analytics  →  Long-run Replay
```

## Immutable research ledger

`research.ledger.HypothesisLedger` is append-only JSONL with no update or
delete path. It records **accepted, rejected, failed, interrupted, and
halted** runs (never just winners), plus:

* run id, git commit, gate result, fingerprints;
* `verify_integrity()` — validates every line without ever rewriting it;
* `find_duplicates()` — groups records with identical research
  fingerprints (strategy + parameters + dataset/config/code fingerprints +
  period). Duplicates are marked `is_duplicate`/`duplicate_of`; pass
  `reject_duplicates=True` to refuse them with `DuplicateExperimentError`.

Daily pipeline runs remain valid: each day is a distinct record, while
identical full research fingerprints are detected.

## MLflow expansion

`research.experiments.ExperimentManager` logs, per run:

* **Parameters** — strategy, strategy version, factor versions, universe,
  factor set, rebalance frequency, cost model, validation method, random
  seed, git commit, dataset version and fingerprint.
* **Metrics** — CAGR, Sharpe, Sortino, volatility, max drawdown, turnover,
  hit rate, deflated-Sharpe probability, gate score/verdict.
* **Artifacts** — equity curve, drawdown series and plot, returns, turnover,
  portfolio weights, validation JSON and fold metrics, confidence intervals,
  validation plot, factor diagnostics, research gate JSON, research report
  JSON/MD. Artifacts are produced by `build_research_artifacts` and logged
  when the MLflow backend supports `log_artifact`.

MLflow remains **metadata-only**: DuckDB stays the research store and
Parquet stays the source of truth. Without MLflow installed (or with a
tracking URI absent) the manager uses a local SQLite backend; with no
MLflow module at all it degrades to the local JSONL audit trail.

## Factor diagnostics

`research.diagnostics` computes, deterministically:

* **factor decay** — per-date cross-sectional IC against forward returns
  at 1/5/21/63-day horizons;
* **sector exposure** — average/final/min/max weight by sector;
* **turnover attribution** — per-symbol and per-side turnover shares;
* **rank stability** — cross-sectional Spearman rank autocorrelation
  between rebalances;
* **volatility contribution** — Euler decomposition of portfolio variance
  per asset, averaged over time (trailing covariance only);
* **factor contribution breakdown** — least-squares attribution of
  portfolio returns to factor portfolios with R² and residual.

## Long-run replay

`research.replay.LongRunReplay` drives a caller-supplied step function over
a deterministic multi-month schedule:

* deterministic scheduling (pure function of dates/frequencies/seed),
* restart recovery (atomic state file after every step),
* duplicate scheduler protection (atomic lock directories; stale locks from
  dead processes are reclaimed),
* configuration-change detection (refuses to resume a different schedule).

## Paper-trading analytics

`research.paper_analytics` is a read-only analytics layer consuming
plain-value fill and position records. It computes realized PnL (FIFO),
unrealized PnL, slippage (bps + currency), exposure, turnover, drawdown,
and benchmark divergence. It is a pure function of its inputs (deterministic
reconciliation) and never imports execution, broker, or risk modules.

## Reports and dashboards

`generate_advanced_report` produces daily/weekly/monthly research reports
with strategy configuration, validation status, benchmark comparison,
confidence intervals, gate outcome, period summaries, and reproducibility
metadata. `dashboard/research_dashboard.py` renders the read-only research
views (leaderboard, validation, factor diagnostics, gate history, timeline,
benchmark comparison) and contains no execution controls.
