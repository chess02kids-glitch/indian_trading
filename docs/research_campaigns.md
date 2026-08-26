# Research campaigns

A **research campaign** (`research/campaign.py`) is the unit of search in
this repository. It exists so the system always knows *how much searching
has already occurred* — the number the multiple-testing corrections depend
on.

## Concepts

- `CampaignStore` — append-only JSONL event log (`reports/generated/.../campaigns.jsonl`).
  Every mutation appends one event; campaign state is replayed from the
  events on load. There is no update or delete path.
- `ResearchCampaign` — replayable snapshot: `campaign_id` (`CMP-00001` …),
  `objective`, `strategy_families`, `research_budget`, status, and trial
  counters (trial_count, active/completed/rejected/failed/
  insufficient_data/duplicate/abandoned/unresolved).
- `ResearchBudget` — conservative caps with defaults
  `max_trials=60`, `max_trials_per_family=12`, `max_parameter_variants=3`.
  Every cap is enforced at reservation time.
- `BudgetExhaustedError` — raised with the message prefix
  `RESEARCH_BUDGET_EXHAUSTED` when any cap would be exceeded.

## Trial lifecycle

```
reserve_trial(campaign, hypothesis_id, family, variant_signature)
    -> MUST happen before the experiment runs
run experiment / evaluate holdout
record_outcome(campaign, hypothesis_id, status)
```

The reservation happens **before** any holdout result exists. The trial
count used by the deflated-Sharpe correction is therefore fixed in advance
— an agent cannot retroactively add or hide trials after seeing results.

Statuses that complete a trial: `accepted`, `rejected`, `failed`,
`insufficient_data`, `duplicate`, `abandoned`. `interrupted`/`halted`
release the slot without a completed verdict (`unresolved_trials`).

## Guarantees

- Trial counts are per-family (`trials_by_family`) and per-parameter-variant
  (`variants_by_family`), so `max_parameter_variants` stops the classic
  "3M momentum, 6M momentum, 12M momentum …" mutation spiral.
- `CampaignStore.can_reserve` is a non-mutating budget probe: nothing is
  consumed by a proposal that cannot run.
- `verify_integrity()` validates the event log; corruption is reported,
  never silently repaired.
- Campaigns are distinguishable in the multiple-testing accounting
  (`research/dsr_accounting.py`): the DSR trial count for a gate decision
  comes from the campaign's reservation count, cross-checked against the
  ledger.

## Statuses

`planned → active → completed | budget_exhausted | paused | abandoned`

## Reporting

`status_report(campaign_id)` returns the full machine-readable state —
budget consumed/remaining, per-family and per-variant counts, exhaustion
flags — and is what the research report, the AI context builder, and the
gate accounting consume.
