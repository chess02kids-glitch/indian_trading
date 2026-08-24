# ADR-006: Paper Trading Before Live Trading

## Context
Transitioning an algorithm from backtesting directly to live capital exposes the user to implementation shortfalls, API bugs, and unmodeled execution dynamics.

## Decision
A mandatory paper-trading layer (`execution/paper.py`) is implemented. All strategies must be run in paper-trading mode before they are authorized for live execution.

## Alternatives Considered
- Direct to live with micro-sizing: Exposes capital to API loop bugs (e.g., placing 10,000 orders of size 1).

## Consequences
- **Pros**: Safe validation of state-machine transitions, reconciliation logic, and broker latency without financial risk.
- **Cons**: Requires maintaining a mock state machine that accurately simulates partial fills and broker rejections.

## Future Review Criteria
Review paper-trading realism periodically against live execution data to refine fill-probability models.
