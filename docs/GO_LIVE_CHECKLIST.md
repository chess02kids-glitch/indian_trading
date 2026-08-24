# RC-1 Go-Live Checklist

**Owner:** ____________________  **Date (UTC):** ____________________
**Release SHA:** ____________________  **Approver:** ____________________

Every item is a mandatory, evidenced check. A checkbox is not evidence: link the
runbook, command output, ticket, or signed approval before enabling any live
execution. RC-1's dashboard is read-only and does not authorize trading.

## Environment and secrets
- [ ] 1. Production host uses a dedicated Ubuntu service account (`quantindia`).
- [ ] 2. `/etc/quant-india/env` exists with mode `0600`.
- [ ] 3. No production `.env` file exists in the repository checkout.
- [ ] 4. `DATABASE_URL` is populated from the approved secret store.
- [ ] 5. Supabase URL and key point to the production project.
- [ ] 6. Broker client IDs, secrets, and redirect URLs are production values.
- [ ] 7. Encryption key is present and has a documented rotation owner.
- [ ] 8. Static egress IP is confirmed with each broker allowlist.
- [ ] 9. Secrets were scanned from Git history and CI logs.
- [ ] 10. Service account has no interactive login and least filesystem privilege.

## Broker authentication and execution safety
- [ ] 11. Upstox authentication succeeds from the VPS egress IP.
- [ ] 12. Dhan authentication succeeds from the VPS egress IP, if enabled.
- [ ] 13. Token refresh is tested before a session expires.
- [ ] 14. Expired sessions fail closed and alert an operator.
- [ ] 15. Broker account number and environment are independently verified.
- [ ] 16. Paper/dry-run credentials are not mixed with production credentials.
- [ ] 17. Every execution path passes through the existing risk engine.
- [ ] 18. Market orders are rejected by configuration and code review.
- [ ] 19. IOC orders are rejected by configuration and code review.
- [ ] 20. Idempotency/duplicate-order protection is verified with a dry run.

## Database, migrations, and RLS
- [ ] 21. `python scripts/verify_migrations.py` passes at the release SHA.
- [ ] 22. Migrations have been rehearsed on a production-like clone.
- [ ] 23. Production migration run has a recorded maintenance window.
- [ ] 24. Migration rollback/forward recovery is documented for each migration.
- [ ] 25. Supabase RLS is enabled on every sensitive table.
- [ ] 26. Authenticated user policies were tested with a non-owner account.
- [ ] 27. Service-role credentials are not exposed to browser code.
- [ ] 28. Audit log inserts and immutability are verified.
- [ ] 29. Database connection retry and timeout behavior are observed.
- [ ] 30. Production schema version is recorded in the release evidence.

## DuckDB, data quality, and backups
- [ ] 31. `DUCKDB_PATH` points to persistent VPS storage.
- [ ] 32. DuckDB free disk space covers database plus two backup generations.
- [ ] 33. Data freshness threshold is configured and stale data fails closed.
- [ ] 34. DuckDB checkpoint/quiesce process is scheduled before file archival.
- [ ] 35. `deploy/backup.sh` creates a timestamped DuckDB archive.
- [ ] 36. Report archive contents are verified after backup.
- [ ] 37. Backup destination has encryption and restricted access.
- [ ] 38. Backup retention and off-host replication are configured.
- [ ] 39. Supabase backup hook is approved, configured, and logged.
- [ ] 40. DuckDB restore drill completed on an isolated host.
- [ ] 41. Report archive restore drill completed and documented.
- [ ] 42. Supabase point-in-time/backup restoration ownership is confirmed.

## VPS, containers, and rollback
- [ ] 43. Ubuntu security updates and time synchronization are current.
- [ ] 44. Docker image build succeeds with Python 3.12.
- [ ] 45. Container runs as non-root and has a passing `/healthz` check.
- [ ] 46. Container has no secrets baked into its image layers.
- [ ] 47. `quant-india.service` is enabled and restart behavior is tested.
- [ ] 48. systemd environment loading is verified after daemon reload.
- [ ] 49. Log rotation runs and preserves appropriate ownership.
- [ ] 50. Disk, memory, process, and file-descriptor limits are monitored.
- [ ] 51. Firewall and reverse-proxy rules expose only approved endpoints.
- [ ] 52. Rollback SHA/image and operator command are written down.
- [ ] 53. Rollback is rehearsed without data loss.

## Monitoring, reconciliation, and release control
- [ ] 54. Dashboard shows broker health from a current status producer.
- [ ] 55. Dashboard unknown values are treated as an alert, never green.
- [ ] 56. Reconciliation runs at the approved cadence.
- [ ] 57. Reconciliation mismatch alerts and blocks new execution.
- [ ] 58. Open orders are independently compared with broker records.
- [ ] 59. Latest experiment ID and artifact are traceable.
- [ ] 60. Kill switch status is visible to on-call staff.
- [ ] 61. Kill switch stops new orders in a controlled dry run.
- [ ] 62. Kill switch cancellation/flatten escalation ownership is confirmed.
- [ ] 63. On-call alert routing and escalation contacts are tested.
- [ ] 64. CI has passed lint, tests, migration verification, security, and Docker build.
- [ ] 65. Production readiness report is regenerated and approved.
- [ ] 66. Final go/no-go meeting records the explicit authorization to enable execution.
