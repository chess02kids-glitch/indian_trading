# Anti-overfitting controls (as implemented)

This document describes **repository behaviour**, not a wish list.

## Frozen v0.6 baseline

`MomentumQualityStrategy` defaults (`lookback=63`, quantiles 0.25/0.50,
monthly rebalance, base costs, holdout 252, CPCV 6/2, seed 20260824)
must not be edited in place. Alternatives are new experiments.

## Search is bounded

`ResearchCampaignStore.authorize_trial` refuses further work with
`RESEARCH_BUDGET_EXHAUSTED`. Defaults: 20 trials, 4 per family, 3
parameter variants.

## History is complete

`HypothesisLedger` statuses include accepted, rejected, failed,
interrupted, halted, invalid, insufficient_data, duplicate, abandoned.
There is no update or delete path.

## Duplicates are explicit

`research.hypothesis.novelty_check` fingerprints family + normalized
parameters + features + transformations + portfolio construction.
Matches return `REJECTED_DUPLICATE` and are recorded, not collapsed.

## Multiple testing (DSR)

`research.gate.ResearchGate` uses `tested_variants` if supplied, else
`len(benchmarks) + len(placebos) + 1`. `ExperimentManager.log_experiment`
uses `len(previous records) + 1`.

Assumptions (do not casually change the formula):

1. Rejected ledger rows **are** previous trials once logged.
2. Benchmarks and placebos are counted in the **gate** trial estimate
   when `tested_variants` is omitted.
3. Campaigns are **not** automatically isolated in DSR; pass
   `tested_variants` from campaign `trial_count` if you need campaign-local
   correction.
4. Trial count must be known **before** interpreting the locked holdout.

See `backtest.validation.deflated_sharpe_from_returns`.

## Holdout

Walk-forward / CPCV run on the development prefix only
(`backtest.validation.run_holdout_protocol`). The gate's OOS evidence is
the untouched holdout slice.

## PIT / survivorship

`rank_eligible` + optional `active_members` on registered strategies.
World F (`research.worlds.world_survivorship`) delists losers late; a
final-universe rank would inflate performance.

## Corporate actions

Known split/bonus/dividend/rename/delisting are applied explicitly.
`MERGER` and `UNKNOWN` raise `UnknownCorporateAction`
(`UNKNOWN_CORPORATE_ACTION`). Nothing is silently repaired.

## Synthetic worlds are not alpha

`research.worlds.FRAMEWORK_VERIFICATION`. Never report World A–G results
as evidence about Indian equities.

## HYP-00002 / real data

Requires the operator fundamentals bundle under `data/bundle/`. If
absent, the honest status is `INSUFFICIENT_DATA`.
