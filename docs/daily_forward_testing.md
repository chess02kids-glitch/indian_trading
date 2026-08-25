# Daily PAPER forward-testing loop

`scripts/run_daily.py` is the sole one-shot daily runner. It accepts only
`QUANT_EXECUTION_MODE=PAPER`, uses the local SQLite state store by default,
and has no network execution client. Input is an explicit CSV or Parquet file
with the standard long OHLCV columns.

```bash
python scripts/run_daily.py --data data/daily/latest.parquet --approved-by operator-name
```

Omit `--approved-by` for the normal safe scheduler behavior: the run validates
data, constructs the plan, writes status, and exits waiting for explicit
approval without submitting orders. An approval identity is intentionally not
stored in systemd units.

Exit codes: `0` completed; `10` duplicate run; `20` data-quality/staleness
halt; `21` risk halt; `22` approval absent/denied; `23` reconciliation lock;
`70` unexpected failure. Every invocation emits one JSON document to stdout
and writes the status document (`QUANT_INDIA_PAPER_STATUS`, default
`var/operational_status.json`). Its `status_document_timestamp` and `fresh`
fields must be checked by dashboard/operator consumers; missing or corrupt
status is unknown, not healthy.

`deploy/systemd/quant-india-daily.timer` schedules the canonical service on
weekdays. The service is `Type=oneshot`, loads `/etc/quant-india/env`, writes
only logs/state directories, and cannot auto-approve a trade plan. Install
only this timer; do not create a competing daily scheduler. Daily logs are
covered by `deploy/logrotate/quant-india-daily`.

A reconciliation mismatch locks the account; stale data, risk states, unknown
approval, malformed inputs, and unexpected errors fail closed. The runner is
paper-only and does not restore a broker-like execution state from a previous
process; same-day reruns are rejected from persistent run state.

## Production Supabase check

Arena cannot reach Supabase. After deployment configuration is available, run
exactly one command:

```bash
python scripts/ops/supabase_smoke.py
```

It emits one secret-free JSON line on stdout and returns non-zero for missing
migrations, connectivity/schema/RLS/rollback failures. Provide that JSON line
to the next session. It never modifies production schema or commits a
transaction.
