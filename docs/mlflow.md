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
