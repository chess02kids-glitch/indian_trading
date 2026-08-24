#!/usr/bin/env bash
# Creates a local DuckDB/report archive and optionally invokes an approved Supabase hook.
set -euo pipefail
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT_DIR"
exec python3 scripts/backup.py "$@"
