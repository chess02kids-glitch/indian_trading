# Row Level Security (RLS) Guide

All tables enforce Row Level Security using PostgreSQL RLS policies in Supabase.

## Access Levels
1. **Anon:** No access to any tables.
2. **Authenticated Users:** Can only SELECT, UPDATE, or INSERT rows where their `auth.uid()` matches the `user_id` on the record.
3. **Service Role:** The backend Python application utilizes a Supabase Service Key, which natively bypasses all RLS checks via the `BYPASSRLS` Postgres attribute.

## Immutability via RLS
The `audit_log` table enforces immutability by completely denying `UPDATE` and `DELETE` commands via RLS to all authenticated users. Triggers that insert into the log operate securely in `SECURITY DEFINER` mode.
