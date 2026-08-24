# ADR-008: Reconciliation as Kill-Switch

## Context
Broker states can drift from internal system states due to missed webhooks, network drops, or partial fills. 

## Decision
Reconciliation (`reconciliation/engine.py`) runs periodically (and mandatory EOD) to compare Supabase positions against Broker live positions. Any discrepancy triggers the global Kill-Switch.

## Alternatives Considered
- Auto-correction: The system automatically places trades to fix the drift. Rejected because it can lead to feedback loops and runaway trading.

## Consequences
- **Pros**: Fail-safe containment of state-drift. Manual human intervention is required to resolve discrepancies, guaranteeing safety.
- **Cons**: System downtime if benign discrepancies (like corporate actions) trigger the switch.

## Future Review Criteria
Review if corporate actions (splits, dividends) cause false positives and require automated adjustments before killing the system.
