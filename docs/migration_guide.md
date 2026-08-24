# Migration Workflow

The repository manages database state using `.sql` migrations inside the `migrations/` folder.

## Automatic Runner
A python script `run_migrations.py` is included to automatically execute all migrations sequentially.
It connects directly to PostgreSQL via `psycopg2`.

### Usage:
```bash
export DATABASE_URL="postgresql://user:password@host:port/postgres"
python migrations/run_migrations.py
```

### Standards
- Migrations must be named with a numerical prefix: `001_schema.sql`, `002_rls.sql`.
- They should ideally be idempotent if they are running raw DDL (e.g. using `IF NOT EXISTS`).
