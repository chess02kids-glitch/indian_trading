# Multiple-testing accounting audit (DSR trial counts)

Audit of the deflated-Sharpe trial-count question, with the answers the
code now enforces (`research/dsr_accounting.py`).

## The questions and answers

1. **What does the gate currently count as "trials"?**
   With campaign context: the campaign's reservation count
   (`trials_source="campaign"`). Without: `config.tested_variants` if set,
   else the legacy heuristic `len(benchmarks) + len(placebos) + 1`
   (`trials_source="heuristic"`), which counts comparators as trials.

2. **Are rejected hypotheses included?**
   Yes — `rejected` is a trial status; it consumed a reservation.

3. **Are benchmark strategies counted?**
   No (campaign/ledger accounting). Benchmarks are comparators, not
   searched hypotheses; counting them would inflate the correction.
   The legacy heuristic did count them; this is documented and labelled.

4. **Are parameter variants counted?**
   Yes — each distinct parameter signature reserved in a campaign is a
   trial, and `max_parameter_variants` additionally caps variants per
   family.

5. **Are separate research campaigns distinguishable?**
   Yes — `trial_count_from_history(ledger, campaign_id=...)` scopes the
   count; `dsr_accounting_report` returns per-campaign counts.

6. **Is the trial count determined before the final holdout evaluation?**
   Yes — by construction: `CampaignStore.reserve_trial` happens before
   the experiment runs; the count is read from the reservation log, and
   the ledger cross-check is reported but never used to alter the count
   after the fact.

## Statuses counted

`accepted, rejected, failed, insufficient_data, duplicate, abandoned,
interrupted, halted, running` — every hypothesis that consumed a campaign
reservation.

**Excluded:** `invalid` (schema/registry rejections never reserve a trial),
benchmarks, placebos.

## Integrity properties

- One trial per hypothesis id: `HypothesisLedger.latest_records()` returns
  the latest record per id (a `running` reservation marker is superseded
  by its outcome record; both lines remain in the append-only log), so
  counters and novelty checks count each trial exactly once.
- Reservation/ledger mismatch is surfaced (`consistent: false`), never
  repaired.
- The gate records `trials_source` in the statistical-confidence check,
  the decision metrics, and the reproducibility block, so every DSR
  number can be traced to its provenance.

## Assumptions (documented, not invented)

- DSR uses the Bailey & López de Prado (2014) approximation
  `E[max SR] ~= (1-γ)Φ⁻¹(1-1/N) + γΦ⁻¹(1-1/(N·e))` with sample
  skewness/kurtosis-adjusted variance (`backtest/validation.py`).
- Trials are treated as independent attempts, as in the paper; correlated
  variant families are bounded separately by the per-family variant cap
  rather than by a correlation-adjusted formula (no such formula is
  implemented, and none was invented).
- Bootstrap intervals (`bootstrap_metric_intervals`) use stationary
  block-style resampling with a fixed seed — deterministic per seed.

## Tests

`tests/test_dsr_accounting.py` covers: loser-counting, campaign scoping,
duplicate/abandoned/running counting, benchmark exclusion, reservation-
before-holdout ordering, mismatch surfacing, gate integration
(`trials_source` provenance), and invalid-trials rejection.
