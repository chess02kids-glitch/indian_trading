#!/usr/bin/env python3
"""Run exactly one fail-closed PAPER forward-test day.

This command has no broker/network execution path.  It intentionally requires
an explicit approval flag for a run that can submit paper orders; scheduled
invocations without that flag stop at ``awaiting_approval``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.idempotency import IdempotencyRegistry
from execution.paper import PaperBroker, PaperBrokerConfig
from execution.service import ExecutionService
from observability.alerts import AlertService
from observability.health import HealthService, SystemHealth
from orchestration.pipeline import DailyPipeline, ManualApprovalGate
from portfolio.construction import EqualWeightConstructor
from reconciliation.engine import ReconciliationEngine
from research.strategies import MomentumStrategy
from risk_kill import RiskGuard, RiskLimits
from store.sqlite import SQLiteStore

EXIT_CODES = {"completed": 0, "duplicate_run": 10, "halted_data_quality": 20,
              "halted_risk": 21, "awaiting_approval": 22,
              "locked_reconciliation": 23, "unexpected_failure": 70}


def _frame(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise ValueError("market data file does not exist")
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise ValueError("market data must be CSV or Parquet")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="validated candidate CSV/Parquet input")
    parser.add_argument("--run-date", default=date.today().isoformat(), help="ISO trading date (UTC)")
    parser.add_argument("--state-db", type=Path, default=Path(os.getenv("QUANT_INDIA_PAPER_DB", "var/paper_daily.sqlite")))
    parser.add_argument("--status-file", type=Path, default=Path(os.getenv("QUANT_INDIA_PAPER_STATUS", "var/operational_status.json")))
    parser.add_argument("--max-staleness-days", type=float, default=float(os.getenv("QUANT_INDIA_MAX_STALENESS_DAYS", "6")))
    parser.add_argument("--approved-by", help="explicit human approver identity; absent is NOT approved")
    return parser.parse_args()


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, default=str))


def main() -> int:
    args = _args()
    try:
        if os.getenv("QUANT_EXECUTION_MODE", "PAPER").upper() not in {"", "PAPER"}:
            raise ValueError("run_daily only permits QUANT_EXECUTION_MODE=PAPER")
        run_date = date.fromisoformat(args.run_date)
        if run_date > date.today():
            raise ValueError("run date cannot be in the future")
        if args.max_staleness_days < 0:
            raise ValueError("max staleness must be non-negative")
        raw = _frame(args.data)
        run_id = f"paper-{run_date.isoformat()}"
        clock = datetime.combine(run_date, time(15, 30), tzinfo=UTC)
        store = SQLiteStore(args.state_db)
        broker = PaperBroker(clock, PaperBrokerConfig(seed=run_date.toordinal()))
        guard = RiskGuard(RiskLimits(max_position_exposure=1.0, max_gross_exposure=1.0))
        service = ExecutionService(broker=broker, order_repository=store.orders,
            position_repository=store.positions, risk_guard=guard,
            idempotency_registry=IdempotencyRegistry())
        health = HealthService(args.status_file)
        pipeline = DailyPipeline(strategy=MomentumStrategy(lookback=2), constructor=EqualWeightConstructor(),
            broker=broker, execution_service=service, risk_guard=guard, run_repository=store.runs,
            position_repository=store.positions, order_repository=store.orders,
            research_repository=store.research, reconciliation_repository=store.reconciliation,
            reconciliation_engine=ReconciliationEngine(guard), health_service=health,
            alert_service=AlertService(), approval_gate=ManualApprovalGate(),
            dataset_version="daily-local", max_staleness_days=args.max_staleness_days)
        result = pipeline.run_day(run_id, raw, approved_by=args.approved_by)
        status = result.to_dict()
        status.update({"run_date": run_date.isoformat(), "started_at": clock.isoformat(),
                       "completed_at": datetime.now(UTC).isoformat(), "current_stage": result.status,
                       "mode": "PAPER", "approval_state": "APPROVED" if result.approved else "NOT_APPROVED",
                       "status_document_timestamp": datetime.now(UTC).isoformat(), "fresh": True,
                       "last_successful_run": run_id if result.status == "completed" else None})
        health.write_extended_status(status)
        _emit(status)
        return EXIT_CODES.get(result.status, EXIT_CODES["unexpected_failure"])
    except Exception as exc:
        payload = {"status": "unexpected_failure", "mode": "PAPER", "failure_reason": str(exc),
                   "status_document_timestamp": datetime.now(UTC).isoformat(), "fresh": True}
        try:
            failed_health = HealthService(args.status_file)
            failed_health.set_state(SystemHealth.HALTED, "daily runner unexpected failure")
            failed_health.write_extended_status(payload)
        except Exception:
            pass
        _emit(payload)
        return EXIT_CODES["unexpected_failure"]

if __name__ == "__main__":
    raise SystemExit(main())
