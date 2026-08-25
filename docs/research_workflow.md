# Production research workflow

The research workflow turns a validated backtest into a scientific
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
Statistical Validation (purge/WF, CPCV) — development prefix only
      ↓
Locked Holdout (single final evaluation + holdout benchmarks/placebos)
      ↓
Research Gate (PASS/FAIL/FRAGILE/INSUFFICIENT_EVIDENCE)
      ↓
Factor Diagnostics  →  Research Reports  →  MLflow (metadata-only)
      ↓
Paper Trading Analytics  →  Long-run Replay
```

## Locked holdout protocol (v0.6)

Evaluation is chronological and hierarchical. The trailing
`--holdout-size` observations (default 252 ≈ 12 months) of the research
timeline are **locked before validation starts**:

* walk-forward and combinatorial-purged CV run on the **development
  prefix only** (`backtest.validation.run_holdout_protocol` raises if any
  fold touches the holdout);
* the gate's out-of-sample evidence is the **single evaluation** of the
  candidate on the untouched holdout slice;
* benchmarks (buy-and-hold, equal weight, inverse volatility,
  persistence, random) and the seeded placebo family are evaluated on
  that same holdout slice — same universe, rebalance schedule, position
  constraints, and cost model — so gate comparisons are like-for-like;
* the explicit boundaries (`dev_start/dev_end/holdout_start/
  holdout_end`, sizes) are recorded in the research report, the gate
  summary, the experiment record, and the hypothesis ledger
  (`holdout_period` field).

The reports therefore make it impossible to confuse a full-period
backtest, a walk-forward fold, the locked holdout, or paper trading:
each carries its own period label and its own metrics.

## Cost-scenario survival

`config.costs` keeps regulatory charges (brokerage, STT, exchange, SEBI,
stamp duty, GST) in a versioned, environment-overridable table;
market-dependent costs (spread, slippage) vary by scenario
(`optimistic` / `base` / `pessimistic`). The baseline experiment
re-simulates the identical weights under all three scenarios and records
full-period and holdout metrics for each, so cost fragility is visible
per period without touching the gate (the gate evaluates the pre-declared
base scenario plus a 2×-cost stress check).

## Versioned universe

The frozen Nifty 100 research snapshot is validated at run time against
the versioned constituent file (`data/universe/nifty100.csv`, with
`valid_from`/`valid_to` membership windows); the run records the
resulting universe version (e.g. `nifty100-snapshot-2023-01-01`) in
every artifact and ledger entry. The snapshot is a **single-date**
constituent view: historical reconstitutions and delistings are not
encoded in it, so results carry a survivorship-bias limitation (see
below). `data.universe.UniverseDataset` exists for membership windows
when point-in-time data becomes available.

## Exact reproduction

```bash
python scripts/run_research_experiment.py \
    --output-dir reports/generated \
    --periods 756 --holdout-size 252 --vol-target 0.15 --seed 20260824
```

The run is deterministic end to end (seeded synthetic data, no network,
seeded bootstrap/placebo generation, seeded git commit): two runs with
the same inputs and code version produce identical research metrics,
gate decisions, and ledger records (timestamps, run ids, and absolute
paths excluded).

## Immutable research ledger

`research.ledger.HypothesisLedger` is append-only JSONL with no update or
delete path. It records **accepted, rejected, failed, interrupted, and
halted** runs (never just winners), plus:

* run id, git commit, gate result, fingerprints;
* backtest/dev/holdout period labels, universe version, and per-period
  metrics (holdout Sharpe/return/drawdown/turnover, walk-forward fold
  consistency, pessimistic-scenario holdout Sharpe);
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

## Known limitations (v0.6)

* **Synthetic data.** The baseline experiment runs on a seeded
  synthetic Nifty 100 price panel so the pipeline is testable without
  network access and is reproducible. Results are a framework
  validation, **not** evidence about real Indian equities. Real-data
  ingestion (`ingestion/` + `data/` quality gates) feeds the same
  contracts once a reliable vendor/source is connected.
* **Survivorship bias.** The universe is a single-date frozen snapshot
  (all constituents `valid_from` 2023-01-01, no delistings). The
  backtest engine refuses to run without an explicit `universe_history`
  declaration, and the limitation is recorded in every run, but this
  snapshot does **not** make the backtest survivorship-bias-free.
* **Corporate actions.** `research.corporate_actions` supports split/
  dividend adjustment, rename, and delisting handling, and the data
  layer distinguishes raw vs adjusted series, but no reliable historical
  Indian corporate-action dataset is wired in yet; adjustment quality
  is therefore not claimed.
* **No alpha claim.** A gate FAIL on the pre-declared baseline is a
  valid result. v0.6 validates that the research infrastructure can
  distinguish signal from noise; it does not demonstrate that the
  momentum strategy has alpha.
