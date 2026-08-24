# Secrets Management

This module guarantees that critical API keys and access tokens are never exposed in plaintext logs, code, or local disk without encryption.

## Configuration Loading
Secrets are loaded strictly from environment variables to support 12-factor app deployment on VPS environments.

### Required Variables
- `QUANT_ENCRYPTION_KEY`: A high-entropy base key used to derive the local Fernet encryption key.
- `UPSTOX_API_KEY` / `UPSTOX_API_SECRET`: Upstox app credentials.
- `DHAN_CLIENT_ID` / `DHAN_API_SECRET`: Dhan app credentials.

## Encrypted Storage Hooks
When a broker's token payload is obtained, it is serialized and encrypted via `cryptography.fernet.Fernet` before being stored at `QUANT_DATA_DIR/sessions/{broker}_session.dat`. 

If `QUANT_ENCRYPTION_KEY` is not set, a warning is logged and the token is saved in plaintext JSON (useful only for local mocked testing, strictly rejected in VPS production).

## Validation
You can manually validate your environment configuration via:

```bash
python -m auth.cli validate
```
This ensures the encryption key is loaded and the static IP whitelist allows traffic.
