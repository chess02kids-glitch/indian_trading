# Anti-overfitting controls

This document describes the actual controls in the repository that prevent
a research system from fooling itself. It answers, for each risk, what
exists and where.

## Multiple-testing correction (DSR)

`backtest/validation.py` implements the Bailey & López de Prado (2014)
deflated Sharpe ratio: the observed Sharpe is corrected for the expected
maximum of `trials` independent attempts, with skewness/kurtosis-adjusted
asymptotic variance.

The critical input is **the trial count**. `research/dsr_accounting.py`
makes that count authoritative search history:

- counted: every hypothesis that consumed a campaign reservation —
  accepted, rejected, failed, insufficient_data, duplicate, abandoned,
  interrupted, halted, running;
- **not** counted: schema-invalid proposals (they never reserve a trial)
  and benchmarks/placebos (they are comparators, not searched hypotheses);
- the count is fixed at **reservation time, before the holdout
  evaluation** exists;
- the campaign reservation count is cross-checked against the ledger;
  mismatches are surfaced, never silently repaired.

`ResearchGate.evaluate(..., trials=..., trials_source=...)` records where
the count came from in the decision's evidence, metrics, and
reproducibility block. The legacy heuristic (`len(benchmarks) +
len(placebos) + 1`) remains only as a documented fallback for callers with
no campaign context; it counts comparators as trials and is labelled
`trials_source="heuristic"`.

See `docs/dsr_accounting.md` for the full audit write-up.

## Research budget

`ResearchBudget` caps search per campaign: `max_trials`,
`max_trials_per_family`, `max_parameter_variants`. Exhaustion raises
`RESEARCH_BUDGET_EXHAUSTED` and flips the campaign status to
`budget_exhausted`. The AI interface probes the budget before allocating
anything.

## Holdout protocol

`backtest/validation.py` provides `holdout_split` and `run_holdout_protocol`
with the frozen configuration (train 252 / test 63 / purge 20 / embargo 5 /
CPCV 6×2). `research/leakage.py::audit_holdout_isolation` verifies the
development and holdout windows are strictly disjoint in time.

## Leakage audits

`research/leakage.py` provides deterministic audits:

- `audit_lookahead` — recomputes a factor on truncated histories and
  compares with the full-data value; any difference means the factor used
  information from after the factor date. **Limitation (documented):**
  truncation-recompute cannot detect a computation that closes over an
  *external* future-information frame; the architectural defence is the
  feature contract — factors must come from the registered factor/strategy
  registry computed on `MarketData` only.
- `audit_rank_mask_order` — verifies (a) no selection ever includes a
  non-member and (b), against a mask-before-rank reference, that the
  ordering was mask-then-rank (a rank-then-mask implementation produces
  different winners even though its final panel contains only members).
- `audit_future_availability` — flags fundamentals rows whose availability
  date is after the as-of reference.
- `audit_survivorship` — flags priced-but-never-eligible symbols and
  membership-before-prices anomalies; delisted-but-still-priced is
  expected for raw panels with PIT masks.
- `audit_holdout_isolation` — dev/holdout disjointness.

## Synthetic worlds

`research/synthetic_worlds.py` provides seven controlled worlds with known
truth (noise, momentum, mean reversion, regime, leakage, survivorship,
multiple testing). They verify the framework where the answer is known —
see `docs/synthetic_worlds.md`. Results on synthetic data are calibration,
never evidence about real markets.

## What is deliberately NOT done

- No grid search: the zoo uses small pre-declared canonical
  configurations.
- No tuning against the holdout: the frozen v0.6 baseline values are
  asserted, not editable; any alternative configuration is a new
  experiment with its own lineage.
- No deletion of failed experiments: the ledger is append-only and records
  every status including `rejected`, `failed`, `insufficient_data`,
  `duplicate`, `invalid`, `abandoned`.
- No strategy can choose its cost model after seeing results: the cost
  model is part of the immutable experiment/config fingerprint.
