# ADR-009: Broker Authentication Architecture

## Context
Brokers like Upstox use daily OAuth 2.0 flows, while Dhan uses static JWTs. Secrets must be managed securely and uniformly without exposing them in logs or code.

## Decision
We abstracted authentication into `auth/oauth.py` and implemented encrypted local caching using `cryptography.fernet`. Secrets are loaded exclusively from `.env`.

## Alternatives Considered
- Plaintext JSON cache: Rejected due to security risks on a VPS if compromised.
- HashiCorp Vault: Overkill and operationally complex for the current scale.

## Consequences
- **Pros**: Unified interface for all brokers. Keys are encrypted at rest.
- **Cons**: CLI requires a manual step (pasting the callback URL) for daily Upstox logins since a local HTTP server isn't viable on headless VPS.

## Future Review Criteria
If fully autonomous zero-touch daily restarts are needed, we may need to automate the OAuth headless browser flow or utilize broker APIs that allow permanent tokens.
