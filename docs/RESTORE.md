# Backup and Restore Runbook

## Create a backup

Schedule a low-write maintenance interval, then run `deploy/backup.sh`. It
creates `backups/quant-india-<UTC timestamp>.tar.gz`, containing the DuckDB file
and the `reports/` directory. Set `DUCKDB_PATH`, `BACKUP_DIR`, and optionally
`SUPABASE_BACKUP_COMMAND` in the service environment. The hook is deliberately
operator-supplied; review its command and credentials before enabling it.

A raw DuckDB file must not be copied during uncontrolled writes. Quiesce writers
or perform the project's approved DuckDB checkpoint procedure first.

## Restore drill

1. Stop application writers and preserve the failed database separately.
2. Verify archive integrity: `tar -tzf backups/quant-india-*.tar.gz`.
3. Extract into an isolated directory: `tar -xzf ARCHIVE -C /srv/restore`.
4. Point a non-production `DUCKDB_PATH` at `/srv/restore/duckdb/...` and run
   validation/read-only queries.
5. Restore reports from `/srv/restore/reports` only after review.
6. For Supabase, use the provider-approved backup/PITR process. Apply migrations
   deliberately; do not run destructive recovery commands from this repository.
7. Reconcile broker positions/orders before resuming any execution. Keep the
   kill switch engaged until the reconciliation is approved.

Record the archive checksum, restore operator, source timestamp, validation
output, and approval in the incident or release ticket.
