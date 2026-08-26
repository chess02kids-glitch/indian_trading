# AUTONOMOUS SESSION REPORT

Quant India — deterministic research-system hardening session
2026-08-26 · branch `arena/01a03b7c-indian-trading`

---

## 1. Starting state

| Item | Value |
| --- | --- |
| Base commit | `04cbbeb` ("checkpoint: rigorous alpha research pipeline") |
| Branch | `arena/01a03b7c-indian-trading` (new branch, pushed) |
| Working tree | clean |
| Test suite | 952 passed, 5 skipped (130 s) |
| Real-data state | `data/bundle/` absent → fundamentals unavailable → HYP-00002 **INSUFFICIENT_DATA** |
| External access | GitHub/PyPI reachable; Yahoo/NSE/BSE not reachable (not retried in a loop) |

## 2. Completed work (12 commits, +8 139 lines)

| Commit | Content |
| --- | --- |
| `3d50ec0` | Campaigns, hypothesis contract, novelty control, lineage (§8–15) |
| `980587d` | Strategy registry + deterministic benchmark zoo (§5–7, §24) |
| `6e87c73` | Authoritative DSR trial accounting wired into the gate (§16) |
| `4f93825` | Synthetic worlds A–G + leakage red-team audits (§17, §24) |
| `ecf0e0a` | AI proposal interface + security boundary (§12–15, §30–31) |
| `cf258aa` | Public API exposure of the v0.8 research system |
| `54b474e` | Documentation (§33): 6 new docs + index + research README |
| `9d23c5b` | PIT universe red-team + corporate-action coverage + **NaN-mask critical fix** (§18–19) |
| `e63c531` | Cost-scenario stress + multi-process determinism tests (§21–22) |
| `bf4e247` | Campaign research summary report (§23) |
| `1c36128` | Backtest/paper consistency audit (§20) |
| (last) | Housekeeping: ruff format fix + regenerated egg-info |

### New modules
- `research/campaign.py` — `CampaignStore` (append-only event log), `ResearchCampaign`, `ResearchBudget`, `RESEARCH_BUDGET_EXHAUSTED`, `can_reserve` budget probe.
- `research/hypotheses.py` — strict pydantic `ResearchHypothesis` (extra=forbid), fingerprints, parameter-variant signatures.
- `research/novelty.py` — deterministic duplicate / near-duplicate controller.
- `research/registry.py` — immutable code-defined `StrategyRegistry` (mapping proxy, parameter bounds, frozen v0.6 canonical config preserved as `momentum_quality`).
- `research/zoo.py` — ten pre-declared families under one methodology; `WeightPanelStrategy` + `IdentityConstructor` for uniform validation.
- `research/dsr_accounting.py` — trial counts from real search history; `trials`/`trials_source` now recorded by the gate.
- `research/synthetic_worlds.py` — worlds A–G with known truth; `scripts/run_synthetic_worlds.py` end-to-end runner.
- `research/leakage.py` — five deterministic audits (lookahead, future availability, rank-mask order, survivorship, holdout isolation).
- `research/ai_research.py` — `AIResearchInterface.submit_proposal` + `ResearchContextBuilder` (no results by default).
- `research/campaign_report.py` — §23 summary report (JSON + Markdown).
- `research/corporate_actions.py` — `audit_corporate_action_coverage`, `UnknownCorporateActionError` (strict mode), `UNKNOWN_CORPORATE_ACTION` surface.

### Extended
- `research/ledger.py` — lineage fields (`campaign_id`, `parent_hypothesis_id`, `strategy_family`, `features`, `transformations`), new statuses (`insufficient_data`, `duplicate`, `invalid`, `abandoned`), `reserve()` atomic id allocation, `latest_records()` deduped view, status counts, lineage chains. Old ledger files remain readable (tested).
- `research/gate.py` — explicit `trials`/`trials_source` with provenance in evidence/metrics/reproducibility.
- `research/strategies.py` — five new zoo families, all sharing the mask-before-rank base.
- `research/__init__.py` — public API for all v0.8 modules.

## 3. Research performed

### Strategy families evaluated (canonical configs, one methodology)
Buy & Hold, Equal Weight, Inverse Volatility, seeded Random placebo, Persistence (one-month-stale momentum), Cross-Sectional Momentum (126d/25%), Trend Following (200d SMA), Quality (composite ROE+D/E, top 50%), Low Volatility (63d realized vol, bottom 25%), Mean Reversion (20d z-score, most-oversold 25%).

### Controlled synthetic worlds (seed 20260824, all gated with walk-forward + 20 placebos + campaign DSR)
| World | Result | Verdict |
| --- | --- | --- |
| A noise | 0/10 families pass | correct |
| B momentum (persistent drift) | cross-sectional momentum, persistence, trend pass (Sharpe 4.5/4.3/4.1) | detected |
| C mean reversion | reversal detects structure (Sharpe 1.28, DSR 1.00, 85% folds) but **rejected on turnover** (16.9x vs 8x) | detection ≠ viability |
| D regime (deterministic bull→bear→recovery) | trend Sharpe 1.20 beats 100% of benchmarks, DSR 1.00, **rejected on fold consistency** (46% vs 50%) | gate is deliberately conservative for lumpy edges |
| E leakage trap | 0/10 pass; leak flagged by audit; external-data smuggling documented as undetectable-by-recomputation (feature-contract defence) | contained |
| F survivorship | PIT selection never includes delisted names; naive full-universe run is worse | contained |
| G multiple testing | variant with Sharpe 0.86 on noise rejected; search stops `RESEARCH_BUDGET_EXHAUSTED` | contained |

### Leakage / PIT / corporate-action red-team
- **Critical bug found & fixed**: `NaN.astype(bool)` is `True` in numpy/pandas. Four sites reindexed membership panels then `.astype(bool).fillna(False)`, so dates/symbols **absent** from the PIT mask became **eligible** — a silent eligibility/look-ahead bug. All sites now `fillna(False).astype(bool)`; regression tests added (missing dates, missing symbols, momentum_quality).
- Rank-then-mask detection: `audit_rank_mask_order` now compares against a mask-before-rank reference and catches the order bug even when the final panel contains only members.
- PIT scenarios tested: join, exit, delist, symbol change, IPO after start, symbol absent from dataset, corporate-action overlap, short membership panels.
- Corporate actions: `UNKNOWN_CORPORATE_ACTION` surfaced; delisting/rename not reflected flagged; strict mode raises; outside-panel informational.

### Statistical / accounting audits
- DSR trial count is now **search history**: reservation-before-holdout, rejected/failed/insufficient-data/duplicate/abandoned/running counted, schema-invalid excluded, benchmarks/placebos excluded, per-campaign scoping, ledger cross-check surfaced. Legacy heuristic retained as labelled fallback only.
- Cost stress: net returns degrade monotonically optimistic→base→pessimistic while turnover/weights are scenario-independent; cost-model version (charge table) appears in gate reproducibility; stressed Sharpe monotonic in cost multiple.
- Determinism: full zoo+gate+placebo pipeline run in separate subprocesses is **bit-identical**.
- Backtest/paper consistency: same registered strategy drives research weights and paper `PortfolioTarget`s; strategies stateless; registry is the shared selection layer.

## 4. Findings

### FACT
- `NaN.astype(bool) == True` in numpy/pandas; the four membership-alignment sites exhibited the resulting silent eligibility bug until fixed (regression tests now pin the behavior).
- World B momentum families pass the full gate (DSR, benchmarks, placebos, walk-forward) on injected persistent drift; world A/E/F/G families do not pass on noise.
- World C reversal and world D trend both produce DSR 1.00 and beat 100% of benchmarks, yet are rejected by turnover (C) and fold consistency (D).
- The gate's legacy trial heuristic counted benchmarks+placebos+1; the campaign/ledger path now replaces it and labels provenance.
- External-data smuggling (a factor closing over a future-information frame) is not detectable by truncation-recompute; the feature contract (registered factors on `MarketData` only) is the defence.
- Migration verification passes; the full suite is green.

### INFERENCE
- The gate separates statistical detection from economic viability (world C) and is deliberately conservative for regime-lumpy edges (world D fold consistency) — a documented property, not a defect.
- Deterministic regime schedules are superior to random-regime generators for calibration worlds: the world's truth holds for every seed.
- The research platform now answers the session's core question: it can evaluate multiple economically distinct ideas under one immutable methodology without fooling itself.

### UNRESOLVED
- HYP-00002 real-data result: **INSUFFICIENT_DATA** — no operator fundamentals bundle in `data/bundle/`.
- Whether real Indian equities contain any of the zoo's signals: cannot be addressed without the fundamentals bundle (or a real-data EOD snapshot; this checkout has neither).
- Correlation structure between parameter variants is bounded by the per-family variant cap, not by a correlation-adjusted multiple-testing formula (none implemented, none invented).
- Fold-consistency threshold (50%) may under-reward lumpy-but-real edges; changing it is a research-methodology decision for the operator.

## 5. Tests

| Gate | Before | After |
| --- | --- | --- |
| `pytest` | 952 passed, 5 skipped | **1133 collected, 1124 passed, 5 skipped** (+172 new tests) |
| `ruff check .` | clean | clean |
| `ruff format --check .` | clean | clean (297 files) |
| `verify_migrations.py` | clean | clean |

New test files: `test_research_campaigns.py` (32), `test_strategy_registry.py` (13), `test_benchmark_zoo.py` (14), `test_dsr_accounting.py` (14), `test_synthetic_worlds.py` (19), `test_leakage_redteam.py` (14), `test_ai_research_boundary.py` (17), `test_pit_universe_redteam.py` (12), `test_campaign_report.py` (5), `test_cost_stress_determinism.py` (6), `test_backtest_paper_consistency.py` (3), plus red-team additions to `test_corporate_actions.py`.

## 6. Real-data status

**HYP-00002 does not exist. `INSUFFICIENT_DATA`.** `data/bundle/` contains no operator-generated fundamentals bundle and this checkout has no eod2 parquet either, so even the price leg of the real-data experiment cannot run here. Nothing was fabricated. The legitimate operator path is unchanged:

```bash
python scripts/ingest_real_data.py --fetch-fundamentals
python scripts/ingest_real_data.py --from-bundle data/bundle
python scripts/run_real_data_experiment.py
```

## 7. Research integrity status

| Question | Answer |
| --- | --- |
| Can failed experiments be hidden? | **NO** — append-only ledger + campaign event log; every status recorded; `latest_records()` dedupes without deleting; no update/delete path |
| Can holdout leak into AI research? | **NO** by default — `ResearchContextBuilder` excludes metrics/gate results unless `include_results=True`; holdout isolation audited |
| Can AI execute arbitrary code? | **NO** — extra=forbid schema, registry-only strategy selection, no exec/eval/pickle/subprocess in the AI path (AST-enforced), static import-graph isolation from gate/backtest/execution/broker/risk |
| Can AI bypass the gate? | **NO** — the interface never calls the gate and never measures; the deterministic engine + gate are the only result producers |
| Can AI change risk controls? | **NO** — risk/execution modules unreachable from the AI import graph |
| Can today's universe contaminate historical research? | **NO** — mask-before-rank enforced in code and by audit; NaN-mask bug fixed; PIT scenarios tested |
| Is research history complete? | **YES** — accepted/rejected/failed/invalid/insufficient_data/duplicate/abandoned all recorded with lineage |
| Is search bounded? | **YES** — `RESEARCH_BUDGET_EXHAUSTED` enforced at reservation, before any evaluation |

## 8. Remaining blockers

1. **Operator fundamentals bundle** (`data/bundle/`) — required for HYP-00002 and for any real-data quality research. Environment cannot fetch it.
2. **Real-data price snapshot** — the eod2 parquet is also absent from this checkout; the operator's `ingest_real_data.py` path produces it.
3. Any decision to change gate thresholds or the fold-consistency rule — a human research-methodology judgment, deliberately not taken.

## 9. Recommended next step

Run the legitimate operator path on a machine with data access:

```bash
python scripts/ingest_real_data.py --fetch-fundamentals
python scripts/ingest_real_data.py --from-bundle data/bundle
python scripts/run_real_data_experiment.py          # HYP-00002, frozen v0.6 baseline
python scripts/run_synthetic_worlds.py --worlds B,D # optional: re-calibrate against real panel
```

Then, once HYP-00002 exists, the highest-value follow-on is: run the ten-family zoo on the real NSE panel (the zoo's `membership` argument is already wired for the committed PIT Nifty-100 universe) and record the families through the campaign machinery — the first real-data benchmark-zoo campaign, gated with honest campaign-scoped DSR accounting. All results must be recorded before any parameter exploration; the frozen v0.6 baseline remains untouchable.
