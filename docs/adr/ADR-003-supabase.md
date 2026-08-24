# ADR-003: Supabase as Transactional Backend

## Context
The platform requires a robust transactional database to track user profiles, API sessions, order states, executions, and audit logs. Managing a raw Postgres instance requires manual backups, connection pooling, and API layers.

## Decision
We chose Supabase (managed PostgreSQL) as the transactional backend, utilizing its built-in Row-Level Security (RLS) and REST/GraphQL APIs via typed Python clients.

## Alternatives Considered
- Raw PostgreSQL: Requires manual setup for connection pooling and API layer.
- SQLite: Unsafe for multi-threaded/multi-process concurrent order execution writes.
- MongoDB: Lacks strict relational constraints needed for financial ledgers (Orders -> Executions).

## Consequences
- **Pros**: Strong relational guarantees, automatic API generation, built-in Auth integration, managed backups.
- **Cons**: Vendor lock-in to Supabase features (RLS, specific client libs).

## Future Review Criteria
If latency to the Supabase cloud instance becomes a bottleneck for execution speeds, we may need to migrate to a self-hosted Postgres instance on the exact same local network as the trading VPS.
