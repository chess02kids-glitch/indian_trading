# MLflow experiment tracking

`research.ExperimentManager` is the tracking boundary. It records both accepted
and rejected experiments and keeps a local JSONL audit trail alongside MLflow.
The default local MLflow backend is SQLite under
`reports/generated/experiments/mlflow.db`, avoiding the deprecated filesystem
tracking backend.

Each run logs:

- hypothesis ID
- strategy name and parameters
- factor set
- universe
- Git commit hash
- start and end timestamps
- numeric backtest metrics
- validation payload, including deflated Sharpe
- benchmark metric payload
- accepted/rejected status and reason

```python
from research import Experiment, ExperimentManager

record = ExperimentManager().log_experiment(
    Experiment("H-001", "momentum", {"lookback": 63}, ["momentum_3m"], "nifty50"),
    result=backtest_result,
    validation=walk_forward_result,
    benchmarks=research_run.benchmarks,
)
```

The experiment ID is a stable hash of hypothesis, strategy, parameters, factor
set, and universe. DSR uses prior local records as the multiple-testing history.
Experiments below the configured probability threshold are marked rejected and
remain queryable in both the MLflow run and local JSONL file.

Set `tracking_uri` to use a managed MLflow deployment. Credentials and service
configuration are injected by the runtime environment, never committed here.

## v0.3 additions

Every research run now also logs:

* **Parameters** — `strategy_version`, `factor_versions` (JSON), `universe`,
  `rebalance_frequency`, `cost_model`, `validation_method`, `random_seed`,
  `git_commit`, `dataset_fingerprint`, `dataset_version`.
* **Metrics** — CAGR (`annualized_return`), `sharpe`, `sortino`,
  `annualized_volatility`, `max_drawdown`, `turnover`, `win_rate`,
  `deflated_sharpe_probability`, plus `gate_score` / `gate_verdict` when a
  research gate decision is logged.
* **Artifacts** — equity curve, drawdown series and Plotly HTML plot,
  returns, turnover, portfolio weights, validation JSON and fold metrics,
  confidence intervals, validation plot, factor diagnostics, research gate
  JSON, and research report JSON/MD. Use
  `research.experiments.build_research_artifacts` to create the set.

MLflow stays metadata-only: the research store remains DuckDB, Parquet
remains the source of truth, and the local JSONL audit trail
(`reports/generated/experiments/experiments.jsonl`) is always written.

The research ledger also records **failed** and **interrupted** runs and
performs duplicate-fingerprint detection (see
`docs/research_workflow.md`).
