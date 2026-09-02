# Secrets Management

This module guarantees that critical API keys and access tokens are never exposed in plaintext logs, code, or local disk without encryption.

## Configuration Loading
Secrets are loaded strictly from environment variables to support 12-factor app deployment on VPS environments.

### Required Variables
- `QUANT_ENCRYPTION_KEY`: A high-entropy base key used to derive the local Fernet encryption key.

### Variables that must **not** be set in LOCAL or PAPER mode

AUDIT-036: `config/env_validator.validate_environment()` refuses to start if
any of these are present in the environment, because a live broker credential
is treated as a live-trading risk. Do **not** follow the older advice of
exporting them:

- `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` — Upstox *app* credentials (not the
  short-lived `UPSTOX_ACCESS_TOKEN`, which is fine and is read-only).
- `DHAN_CLIENT_ID` / `DHAN_API_SECRET` — Dhan app credentials.

They are held only in the operator's own secret store and are loaded in a
VPS/PRODUCTION deployment, never in a developer shell or a paper run.

## Encrypted Storage Hooks
When a broker's token payload is obtained, it is serialized and encrypted via `cryptography.fernet.Fernet` before being stored at `QUANT_DATA_DIR/sessions/{broker}_session.dat`. 

If `QUANT_ENCRYPTION_KEY` is not set, a warning is logged and the token is saved in plaintext JSON (useful only for local mocked testing, strictly rejected in VPS production).

## Validation
You can manually validate your environment configuration via:

```bash
python -m auth.cli validate
```
This ensures the encryption key is loaded and the static IP whitelist allows traffic.
