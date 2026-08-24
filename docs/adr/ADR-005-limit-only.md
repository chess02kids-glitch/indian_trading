# ADR-005: LIMIT-only Execution Policy

## Context
Market orders guarantee execution but not price, exposing the algorithm to unbounded slippage, especially in illiquid Indian equity/derivatives markets.

## Decision
The system enforces a strict LIMIT-only execution policy. All orders must specify a target price. Market and IOC (Immediate-Or-Cancel at market) orders are programmatically rejected by the risk engine.

## Alternatives Considered
- Permitting Market orders for stop-losses: Rejected due to flash-crash risks. Synthetic stop-limits are preferred.

## Consequences
- **Pros**: Guaranteed maximum slippage bounds. Erroneous price calculations fail to execute rather than executing at ruinous prices.
- **Cons**: Risk of partial fills or non-execution if the market moves away rapidly.

## Future Review Criteria
Re-evaluate if liquidity profiles of traded assets guarantee less than 0.01% slippage and speed is prioritized over price certainty.
