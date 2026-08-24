# ADR-010: RC-1 Deployment Architecture

## Context
The system has reached RC-1 and needs to be deployed securely for live paper/production trading.

## Decision
The system is deployed on a single Linux VPS using Docker Compose. The environment runs as a non-root user. Systemd is used to manage auto-restarts and environment variable injection.

## Alternatives Considered
- Kubernetes: Unnecessary complexity for a single-node trading monolith.
- Serverless (AWS Lambda): Unsuitable for long-running websocket connections and stateful execution engines.

## Consequences
- **Pros**: Simple, reproducible deployments. Container isolation.
- **Cons**: Single point of failure (the VPS).

## Future Review Criteria
If we require multi-region failover or distributed execution, migration to Kubernetes or AWS ECS will be evaluated.
