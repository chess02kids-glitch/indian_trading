# VPS Deployment Infrastructure

This guide outlines the production deployment infrastructure for Phase C (Realtime and Supabase integrations).

## 1. Prerequisites
- **PostgreSQL Client**: Ensure `pg_dump` is installed for the automated backups (`sudo apt-get install postgresql-client`).
- **Supabase**: Remote database provisioned, `DATABASE_URL` accessible.
- **Environment**: Define `SYSTEM_MODE=PRODUCTION`.

## 2. Startup Validation
On system boot, `run_migrations.py` runs idempotently to apply any pending SQL migrations (e.g. `006_research_infrastructure.sql` and `007_realtime_subscriptions.sql`).

## 3. Health Monitoring
A minimal health endpoint runs on `:8080/health` using `scripts/health_server.py`. 
It executes the same pre-flight validations (`validate_database_health()`) as the core engine, assuring the VPS Load Balancer/Docker health check that the process can see the database securely.

## 4. Structured Logging
The system now uses `config/logging.py` to output fully structured JSON lines (`ELK`, `Datadog` compatible) instead of standard terminal text formatting. Set `LOG_LEVEL` environment variable appropriately.

## 5. Automated Backups & Systemd
We use systemd units for robust deployment (found in `deploy/` directory).

- `deploy/health.service`: Runs the health server on port 8080 continuously.
- `deploy/backup.service` & `deploy/backup.timer`: Executes `scripts/backup_db.py` nightly.

Backups are now encrypted with AES (via `BACKUP_ENCRYPTION_KEY`) and signed with a SHA-256 checksum.

## 6. Disaster Recovery (Restore)
To recover from a disaster, use the `scripts/restore_db.py` utility.
This will automatically decrypt `.enc` files and stream them to `psql`.
```bash
# Decrypts and restores the backup to DATABASE_URL
python scripts/restore_db.py backups/supabase_backup_20240101_120000.sql.enc
```

## 6. Realtime Subscriptions
To connect to the realtime telemetry (Health states, experimental results, and reconciliation logs), initialize the client via `store.realtime.get_realtime_client()`, assign your event handlers, and run `start_listening()`.
