# ADR-004: Risk-First Execution Architecture

## Context
Algorithmic trading systems can suffer catastrophic financial losses if a logic bug repeatedly fires orders. Execution speed must be balanced with absolute safety.

## Decision
Execution *must always* pass through the Risk Engine. The architecture explicitly isolates signal generation from execution, placing the Risk Engine as the mandatory middleware gateway.

## Alternatives Considered
- Strategy-embedded risk: Strategies check their own risk. Rejected because a bug in the strategy could bypass the check.
- Post-trade risk analysis: Rejected because it does not prevent the trade from occurring.

## Consequences
- **Pros**: Centralized choke-point for all orders. A global kill-switch immediately halts all system output.
- **Cons**: Marginal latency penalty (microseconds) for in-memory risk checks.

## Future Review Criteria
Review risk check latency if High-Frequency Trading (HFT) sub-millisecond execution becomes a requirement.
