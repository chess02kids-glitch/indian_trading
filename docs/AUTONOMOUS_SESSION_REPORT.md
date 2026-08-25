# AUTONOMOUS SESSION REPORT

## 1. Starting state

* Repository: `/home/user/indian_trading`
* Branch: `arena/01a039dd-indian-trading` (session-fixed; not `arena/01a038e7`)
* Checkout commit: `15dc4ac` (merge of PR #7 / prior v0.7 work)
* Working tree was clean
* `data/bundle/` contains only `.gitkeep` — **no operator fundamentals bundle**
* Arena cannot reach Yahoo/NSE/BSE for a live fundamentals fetch
* Frozen v0.6 strategy/config **not modified**

## 2. Completed work

New / extended modules:

* `research/campaign.py` — `ResearchCampaign`, append-only `ResearchCampaignStore`, `ResearchBudgetExhausted`
* `research/campaign_report.py` — campaign summary (tested/failed/insufficient/passed/unexplored)
* `research/hypothesis.py` — Pydantic `ResearchHypothesis`, fingerprints, `novelty_check`
* `research/registry.py` — 10-family benchmark zoo, one canonical config each
* `research/pit.py` — mask-then-rank (`rank_eligible`)
* `research/worlds.py` — synthetic worlds A–G labelled `FRAMEWORK_VERIFICATION`
* `research/ai_boundary.py` — schema validation, no-exec check, context builder, `submit_hypothesis`
* `research/ledger.py` — statuses `invalid`, `insufficient_data`, `duplicate`, `abandoned`; lineage fields
* `research/corporate_actions.py` — `MERGER`/`UNKNOWN` raise `UNKNOWN_CORPORATE_ACTION`

Docs: `docs/research_campaigns.md`, `docs/strategy_registry.md`, `docs/anti_overfitting.md`, `docs/ai_research_boundary.md`

Tests: `tests/test_research_hardening.py`

## 3. Research performed

* **Strategy families registered (not optimized):** buy & hold, equal weight, inverse vol, random/placebo, persistence, momentum, trend, quality, low vol, mean reversion
* **Worlds:** A noise, B momentum, C mean-reversion, D regime, E leakage trap, F survivorship trap, G multiple-testing trap
* **Leakage:** World E future-return feature is flagged by `future_information_present`
* **PIT:** World F + `rank_eligible` keep delisted names out of the rank distribution
* **DSR accounting audit (code inspection, no formula change):**
  * Gate default trials = `tested_variants` or `len(benchmarks)+len(placebos)+1`
  * Experiment manager DSR uses `len(previous local records)+1`
  * Rejected rows count once logged
  * Campaigns are **not** auto-isolated; pass `tested_variants` from campaign `trial_count` if needed
* **No real-data experiment** — bundle missing

## 4. Findings

### FACT

* Fundamentals bundle is absent → real-data path cannot run.
* HYP-00002 was **not** generated.
* Frozen MomentumQualityStrategy defaults were not edited.
* Unknown/merger corporate actions now fail closed.
* Duplicate hypotheses fingerprint family + normalized parameters + features + transforms + constructor.
* Campaign authorize path emits `RESEARCH_BUDGET_EXHAUSTED`.

### INFERENCE

* World B’s last-bar long names are concentrated in persistent high-drift symbols — consistent with the injected DGP, **framework only**.
* Default DSR trial counts can under-count intra-campaign parameter search unless `tested_variants` is wired from the campaign.

### UNRESOLVED

* Real Nifty fundamentals / HYP-00002
* Whether paper execution and research share every cost/turnover edge case (audited architecture only; no live path)
* Full 952-test suite not re-run here: environment has Python 3.11, project requires 3.12, and vectorbt/mlflow extras were not fully installed

## 5. Tests

This session (Python 3.11 venv, subset that the sandbox can import):

```
tests/test_research_hardening.py
tests/test_ledger.py
tests/test_corporate_actions.py
tests/test_research_factors.py
tests/test_quality_factors.py
tests/test_universe_dataset.py
tests/test_holdout_protocol.py
→ 80 passed
```

Ruff: clean on the new/changed research files (local venv ruff).

Not re-run in this environment: full `pytest`, `ruff check .`, `ruff format --check .`, `verify_migrations.py` (missing 3.12 / full extras).

## 6. Real-data status

**INSUFFICIENT_DATA**

HYP-00002 does **not** exist. No Sharpe, gate, or alpha number is claimed.

Operator path remains:

```bash
python scripts/ingest_real_data.py --fetch-fundamentals
python scripts/ingest_real_data.py --from-bundle data/bundle
python scripts/run_real_data_experiment.py
```

## 7. Research integrity status

| Question | Answer |
| --- | --- |
| Can failed experiments be hidden? | **NO** (append-only ledger; losers have statuses) |
| Can holdout leak into AI research? | **NO** for return series via `build_research_context` (history/metrics only). Holdout isolation still depends on callers using `run_holdout_protocol`. |
| Can AI execute arbitrary code? | **NO** (`submit_hypothesis` + registry; no exec path) |
| Can AI bypass the gate? | **NO** (submit does not backtest or gate-pass) |
| Can AI change risk controls? | **NO** (forbidden modules; no risk API) |
| Can today's universe contaminate historical research? | **NO** if `rank_eligible` / `active_members` are used. Frozen snapshot universes remain a documented survivorship limitation. |
| Is research history complete? | **YES** for statuses listed; nothing is deleted |
| Is search bounded? | **YES** when campaigns are used |

## 8. Remaining blockers

* Operator fundamentals bundle still missing
* Full CI suite needs Python 3.12 + project extras
* Real-data HYP-00002 still blocked

## 9. Recommended next step

Place a validated fundamentals bundle in `data/bundle/` and run the **frozen** real-data experiment script **once**, without changing v0.6 parameters. Record whatever the gate says — including FAIL / INSUFFICIENT_EVIDENCE.

Then, if desired, run the **new** registry zoo as a **separate campaign** on the same PIT dataset (not as a retune of HYP-00001/00002).
