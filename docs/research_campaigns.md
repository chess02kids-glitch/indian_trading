# Research campaigns

A **campaign** (`research.campaign.ResearchCampaign`) is the unit of search
accounting. It answers: how many ideas have already been tested?

## Identity

* `campaign_id` — `CMP-00001` style
* `objective` — why this search exists
* `strategy_families` — families already touched
* counters: `trial_count`, `active_trials`, `completed_trials`,
  `rejected_trials`, `accepted_candidates`, `insufficient_data_trials`,
  `duplicate_trials`, `failed_trials`, `invalid_trials`, `abandoned_trials`
* budget: `max_trials` (default 20), `max_trials_per_family` (4),
  `max_parameter_variants` (3)
* `status` — `draft` / `active` / `budget_exhausted` / `completed` / `abandoned`

## Persistence

`ResearchCampaignStore` is append-only JSONL. Mutations write a new snapshot.
History is never deleted. `latest(id)` is the last snapshot.

## Budget

`authorize_trial` reserves one trial or raises `ResearchBudgetExhausted`
with the token `RESEARCH_BUDGET_EXHAUSTED`. The store does not re-interpret
the ledger: the caller supplies `family_trial_count` and
`parameter_variant_count` from history.

`resolve_trial` decrements `active_trials` and increments the matching
outcome counter.

## Summary

`research.campaign_report.campaign_summary_report` reports tested / failed /
insufficient-data / passed / unexplored families. It is not a Sharpe
leaderboard.

Campaigns do not run backtests and do not talk to brokers.
