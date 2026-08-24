# ADR-007: MLflow Experiment Tracking

## Context
Quantitative research involves testing hundreds of hypotheses (features, hyperparameters, models). Tracking this manually in spreadsheets leads to lost knowledge and irreproducible results.

## Decision
We adopted MLflow for all research experiment tracking, logging parameters, metrics (Sharpe, Max Drawdown), and model artifacts.

## Alternatives Considered
- Weights & Biases: Excellent, but cloud-hosted. MLflow allows local file-based or Supabase-backed tracking for privacy.
- TensorBoard: Too focused on deep learning; poor support for backtest portfolio metrics.

## Consequences
- **Pros**: Reproducible research, easy comparison of hypotheses, integration with Supabase `experiments` table.
- **Cons**: Additional dependency overhead in the research environment.

## Future Review Criteria
Re-evaluate if the team scales to a point where a managed MLflow server or W&B enterprise is required for collaboration.
