# Quant India - Project Context (RC-1)

## Vision
Quant India is an institutional-grade, automated algorithmic trading platform targeting Indian Equities and Derivatives. It bridges the gap between rigorous quantitative research and robust, risk-managed live execution.

## Core Tenets
1. **Safety First**: Capital preservation overrides all other concerns. The system utilizes LIMIT-only orders, strict kill-switches, and a mandatory risk-engine gateway.
2. **Reproducibility**: Research is tracked meticulously via MLflow and combinatorial cross-validation. Data is immutable.
3. **Architecture over Ad-hoc**: Features are implemented via strict repository patterns, abstractions, and typed interfaces.

## Current State (RC-1)
The repository is at Release Candidate 1.
- **Data**: Supabase handles transactional state (Users, Orders, Executions, API Sessions). DuckDB handles analytical workloads on immutable Parquet files.
- **Research**: VectorBT PRO handles backtesting with custom factor models and CP-CV validation.
- **Execution**: A state-machine manages order lifecycles. Broker integrations (Upstox, Dhan) are abstracted.
- **Operations**: CLI tooling exists for authentication, system health, and migrations.

## Next Phase
Post RC-1, the focus will shift from infrastructure scaffolding to strategy implementation, live forward-testing (paper trading), and latency optimizations.
