# Infrastructure Security & Migration Audit
Generated at: 2026-08-25T09:21:00.998192

## Migrations
- 001_initial_schema.sql (Applied: 2026-08-25 02:08:46.263270+00:00)
- 002_rls_policies.sql (Applied: 2026-08-25 02:08:46.263270+00:00)
- 003_audit_log.sql (Applied: 2026-08-25 02:08:46.263270+00:00)
- 004_transactional_boundaries.sql (Applied: 2026-08-25 02:08:46.263270+00:00)
- 005_audit_hash_chaining.sql (Applied: 2026-08-25 02:08:46.263270+00:00)
- 006_research_infrastructure.sql (Applied: 2026-08-25 02:08:48.273344+00:00)
- 007_realtime_subscriptions.sql (Applied: 2026-08-25 02:08:48.273344+00:00)
- 008_production_hardening.sql (Applied: 2026-08-25 03:47:05.155761+00:00)

## Row Level Security
### Table: api_sessions
- RLS Enabled: Yes
  - Policy: Users can delete their own API sessions (Cmd: DELETE, Roles: ['authenticated'])
  - Policy: Users can insert their own API sessions (Cmd: INSERT, Roles: ['authenticated'])
  - Policy: Users can view their own API sessions (Cmd: SELECT, Roles: ['authenticated'])
### Table: audit_log
- RLS Enabled: Yes
  - Policy: Deny deletes on audit_log (Cmd: DELETE, Roles: ['authenticated'])
  - Policy: Deny updates on audit_log (Cmd: UPDATE, Roles: ['authenticated'])
### Table: audit_log_state
- RLS Enabled: No
### Table: datasets
- RLS Enabled: Yes
  - Policy: Allow auth access to datasets (Cmd: ALL, Roles: ['authenticated'])
### Table: executions
- RLS Enabled: Yes
  - Policy: Users can view their own executions (Cmd: SELECT, Roles: ['authenticated'])
### Table: experiments
- RLS Enabled: Yes
  - Policy: Authenticated users can view experiments (Cmd: SELECT, Roles: ['authenticated'])
### Table: health_state
- RLS Enabled: Yes
  - Policy: Allow auth access to health_state (Cmd: ALL, Roles: ['authenticated'])
### Table: order_attempts
- RLS Enabled: Yes
  - Policy: Users can insert their own order attempts (Cmd: INSERT, Roles: ['public'])
  - Policy: Users can update their own order attempts (Cmd: UPDATE, Roles: ['public'])
  - Policy: Users can view their own order attempts (Cmd: SELECT, Roles: ['public'])
### Table: orders
- RLS Enabled: Yes
  - Policy: Users can insert their own orders (Cmd: INSERT, Roles: ['authenticated'])
  - Policy: Users can update their own orders (Cmd: UPDATE, Roles: ['authenticated'])
  - Policy: Users can view their own orders (Cmd: SELECT, Roles: ['authenticated'])
### Table: positions
- RLS Enabled: Yes
  - Policy: Users can view their own positions (Cmd: SELECT, Roles: ['authenticated'])
### Table: reconciliation_log
- RLS Enabled: Yes
### Table: schema_migrations
- RLS Enabled: No
### Table: universe_history
- RLS Enabled: Yes
  - Policy: Allow auth access to universe_history (Cmd: ALL, Roles: ['authenticated'])
### Table: users
- RLS Enabled: Yes
  - Policy: Users can update their own profile (Cmd: UPDATE, Roles: ['authenticated'])
  - Policy: Users can view their own profile (Cmd: SELECT, Roles: ['authenticated'])
