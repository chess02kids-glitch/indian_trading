# Deployment Flow

## 1. Environment Preparation
- Provision an Ubuntu VPS.
- Assign a Static IP (whitelist in Broker portals).
- Setup `.env` securely (do not commit to git).
  - Requires `SUPABASE_URL`, `QUANT_ENCRYPTION_KEY`, API keys.

## 2. Infrastructure Setup
- Install Docker and Docker Compose.
- Apply Supabase migrations: `python -m migrations.run_migrations`
- Validate environment: `python -m auth.cli validate`

## 3. Deployment
- Clone the repository (or pull latest `main`).
- Build image: `docker compose build`
- Start services: `docker compose up -d`
- Check health: `python -m auth.cli status`

## 4. Daily Operations
- The system is managed via systemd timers or cron for daily ingestion and EOD reconciliation.
- Authentication for brokers requiring daily login is executed via `python -m auth.cli upstox`.
