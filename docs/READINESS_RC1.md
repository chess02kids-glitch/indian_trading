# RC-1 Production Readiness Report

**Release candidate:** RC-1<br>
**Generated:** 2026-08-24 UTC<br>
**Scope:** deployment hardening only; no new trading feature or broker API was added.

## Local verification recorded

The focused RC-1 test suite completed successfully on 2026-08-24: **14 passed**.
The full suite remains a required CI gate on Python 3.12.

## Evidence to approve

| Gate | Evidence command | Expected result |
| --- | --- | --- |
| Unit and dry-run suite | `pytest` | Pass |
| Formatting and lint | `ruff check . && ruff format --check .` | Pass |
| Migration gate | `python scripts/verify_migrations.py` | Pass, static only |
| Container | `docker build -t quant-india:rc1 .` | Build and `/healthz` response |
| Backup drill | `deploy/backup.sh` then `tar -tzf backups/*.tar.gz` | DuckDB and reports present |

## Automated dry-run coverage

The isolated fake-broker suite in `tests/test_release_dry_run.py` covers a full
LIMIT-order lifecycle, stale data rejection, duplicate rejection, session
expiry, broker failure, and reconciliation mismatch. It deliberately creates
no Market or IOC orders and makes no external network calls. Each scenario
fails closed before or at the simulated broker boundary.

## RC-1 operational artifacts

- Docker image: Python 3.12, non-root `quant` user, read-only dashboard entrypoint, healthcheck.
- Ubuntu VPS: systemd unit, environment file, restart helper, and log rotation.
- CI: pip and Docker cache, migration gate, test artifact, dependency audit, Bandit, and Docker build.
- Backups: timestamped DuckDB/report archive plus an explicit, operator-owned Supabase backup hook.
- Dashboard: read-only broker health, reconciliation, kill switch, latest experiment, open orders, and system health values. Missing status is `unknown`.

## Approval constraints

This report is **not authorization to trade**. Before production execution,
complete and sign `GO_LIVE_CHECKLIST.md`; test broker credentials from the VPS;
validate Supabase RLS and migrations against the real production project;
perform a restore and reconciliation drill; and obtain the named release
approver's explicit go-live decision.
