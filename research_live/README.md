# Live Research Engine — Indian Equities

Evidence-driven strategy research on NSE daily OHLCV (2009-2026). Only one
strategy survived institutional validation: **MomReM** (Momentum + Market-Regime
Filter).

## Reproduce
```bash
python -m venv /home/user/venv
/home/user/venv/bin/pip install pandas numpy scipy pyarrow duckdb matplotlib

# Data sweep (family-by-family IS/OOS)
/home/user/venv/bin/python research_live/research_main.py

# Winner validation on the broad liquid universe
/home/user/venv/bin/python research_live/broad_liquid_validate.py
/home/user/venv/bin/python research_live/liquidity_spectrum.py   # edge vs liquidity

# Full deliverables (strategy card, report, charts)
/home/user/venv/bin/python research_live/final_report.py
```

## Deliverables (`research_live/deliverables/`)
- `STRATEGY_REPORT.md` — strategy card + full validation report
- `equity_drawdown.png` — equity & drawdown curves (strategy vs benchmark)
- `param_heatmap.png` — OOS Sharpe over the (lookback × regime-MA) grid
- `trade_dist.png` — distribution of monthly returns
- `equity.csv`, `drawdown.csv`, `yearly.csv`

## Source modules
- `data.py` — clean large-cap parquet loader
- `broad_data.py` — broad split-adjusted universe (daily CSVs + corporate actions)
- `engine.py`, `metrics.py`, `alpha.py` — simulator, metrics, CAPM alpha
- `strategies.py` — strategy families
- `validate.py`, `final_report.py` — validation & reporting

See `RESEARCH_LOG.md` for the full experiment-by-experiment journal (including
every rejected family and the look-ahead/liquidity issues discovered).
