# Database Schema Guide

This document covers the transactional database schema implemented on Supabase (PostgreSQL).

## Tables
- `users`: Core identity table for traders and admins.
- `api_sessions`: Tracks broker session tokens linked to users.
- `orders`: Transactional store for pending, open, and filled orders. Uses an `order_status` ENUM constraint.
- `executions`: Read-only (via trigger) ledger of broker-side fills. Linked to `orders`.
- `positions`: Holds the current realized and unrealized net positions per user.
- `reconciliation_log`: Tracks discrepancies between DB positions and Broker positions.
- `experiments`: Stores backtest results and MLFlow model hypothesis mappings.
- `audit_log`: Immutable ledger capturing `INSERT/UPDATE/DELETE` payloads across all critical tables.

All tables use `UUID` for primary keys and maintain automated `updated_at` timestamps via triggers.
