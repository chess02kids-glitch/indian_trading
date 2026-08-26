# Synthetic controlled worlds

`research/synthetic_worlds.py` + `scripts/run_synthetic_worlds.py` verify
the research framework on data where the **truth is known**. Results on
these worlds are calibration — never evidence about real Indian equities.

## The worlds

| World | Structure injected | Framework expectation |
| --- | --- | --- |
| A — pure noise | iid Gaussian returns | no family reliably passes the gate |
| B — momentum | per-symbol permanent drift `m_i`, `r_t = m_i + ε` | cross-sectional momentum / persistence detect it |
| C — mean reversion | log prices pulled toward their 20-day mean | reversal detects it (but see the turnover finding) |
| D — regime | deterministic two-regime market schedule (bull → bear → recovery) | trend following beats naive passive |
| E — leakage trap | noise prices + declared `next_day_return` leak feature | lookahead audit flags any factor using it |
| F — survivorship | negative-drift symbols delisted at staggered dates | PIT membership removes the artificial boost |
| G — multiple testing | noise + unbounded random-variant factory | budget + DSR prevent promotion |

Worlds are deterministic: same `world_id` + `seed` → identical price
panels and truths, fingerprinted via `SyntheticWorld.fingerprint()`.
World D uses a deterministic regime *schedule* (hidden from the strategy —
it only sees prices).

## Runner

```bash
python scripts/run_synthetic_worlds.py            # worlds A–G
python scripts/run_synthetic_worlds.py --worlds B,D
```

For each world: a campaign is created, every zoo family's trial is
reserved **before** any evaluation, the ten-family zoo runs, each family
is gated against the other families (walk-forward validation, 20 seeded
placebos, DSR trials = campaign search count), and every outcome is
recorded in the ledger and campaign store. Outputs (JSON + Markdown) go to
`reports/generated/synthetic_worlds/` (git-ignored). World G also
demonstrates `RESEARCH_BUDGET_EXHAUSTED` end-to-end.

## Findings from the canonical run (seed 20260824)

| World | Outcome | Interpretation |
| --- | --- | --- |
| A | 0/10 families pass | the gate does not manufacture alpha on noise |
| B | csm, persistence, trend pass | injected momentum is detected and survives DSR, benchmark, placebo, and validation checks |
| C | reversal detected: Sharpe 1.28, DSR 1.00, beats 100% of benchmarks, 85% folds positive — **rejected on turnover** (16.9x vs 8x limit) | the gate separates statistical detection from economic viability |
| D | trend Sharpe 1.20 beats 100% of benchmarks, DSR 1.00 — **rejected on fold consistency** (46% vs 50%) | the edge is regime-lumpy; short-window fold consistency underestimates lumpy edges (deliberate conservatism) |
| E | 0/10 pass without the leak; leak flagged by audit | leakage cannot flow through the standard contract |
| F | PIT selection never includes delisted names; naive full-universe run is worse | PIT handling removes the survivorship boost |
| G | variant Sharpe 0.86 on noise **rejected**; search stops with `RESEARCH_BUDGET_EXHAUSTED` | budget + DSR defeat the multiple-testing trap |

These findings are about the framework, not about markets.
