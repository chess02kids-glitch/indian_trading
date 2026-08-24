# Disaster Recovery Playbook

## Scenario 1: Reconciliation Discrepancy (State Drift)
**Trigger**: EOD reconciliation detects a mismatch between Supabase and the Broker. Kill-switch activated.
**Action**:
1. Check `reconciliation_log` in Supabase for discrepancy details.
2. Manually verify positions on the broker terminal.
3. If internal DB missed an execution, insert it manually via Supabase studio.
4. If broker executed an orphaned order, manually close the position on the broker terminal.
5. Reset the kill-switch ONLY when states match perfectly.

## Scenario 2: Supabase Outage
**Trigger**: Connection timeouts; `with_retries` exhausts attempts.
**Action**:
1. The execution engine will fail-safe and refuse new orders.
2. Check Supabase status page.
3. If prolonged outage, rely on broker terminal to flatten positions manually.

## Scenario 3: VPS Compromise / Secret Leak
**Trigger**: Unauthorized access detected.
**Action**:
1. Immediately regenerate `QUANT_ENCRYPTION_KEY`.
2. Revoke all API keys in the Broker developer portals.
3. Rotate Supabase database password.
4. Nuke `.sessions/` directory to destroy compromised encrypted tokens.
