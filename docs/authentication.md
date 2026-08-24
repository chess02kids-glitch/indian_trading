# Broker Authentication Guide

Quant India supports secure OAuth flow integrations with Upstox and Dhan for API access.

## Architecture

The authentication infrastructure is abstracted through `auth.oauth.OAuthFlow`. This ensures uniform access regardless of broker-specific idiosyncrasies.

- **Upstox**: Uses OAuth 2.0 authorization code flow. Because their refresh token policy requires daily logins, we mock a daily renewal in our system until official offline refresh is supported.
- **Dhan**: Traditionally uses static API JWTs. For consistency, our abstraction treats this as a long-lived token exchange.

## Command Line Usage

Once secrets are in `.env`, you can initiate login flows from the terminal. Note that the command currently stands alone, but will later be mounted under the main CLI once the Safety Layer is merged.

```bash
# Initiate Upstox login
python -m auth.cli upstox

# Initiate Dhan login
python -m auth.cli dhan
```

The CLI will generate a URL for you to authorize the application. Once authorized, you paste the redirected callback URL (e.g. `http://localhost:8080?code=xyz...`) back into the terminal.

## Session Management

`auth.session.SessionManager` automatically persists sessions:
1. **Local Disk**: Encrypted local caching via Fernet for rapid access.
2. **Supabase**: Written to the `api_sessions` table for cross-node synchronization and audit logging.

If a token is expired, `SessionManager.get_valid_session("upstox")` will automatically attempt a refresh (using the refresh token) before returning the payload.
