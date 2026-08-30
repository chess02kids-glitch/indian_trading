#!/usr/bin/env python
"""Generate sample data for testing the Quant India Dashboard.

This script creates sample files in the expected locations so the dashboard
can be tested without a full data pipeline.

Usage:
    python scripts/generate_sample_data.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def generate_health_check() -> dict:
    """Generate sample health_check.json."""
    return {
        "state": "PAPER",
        "reason": "paper trading active",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_run_ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "data_age_min": 5,
        "broker_last_ping_min": 2,
        "daily_loss_used": 0,
        "daily_loss_limit": 10000,
        "current_drawdown": 0.02,
        "max_drawdown_limit": 0.2,
        "orders_this_second": 0,
        "rate_cap": 10,
        "max_position_concentration": 0.15,
        "concentration_limit": 0.25,
        "token_expiry": (datetime.now(timezone.utc).timestamp() + 3600 * 24) * 1000,  # 24 hours from now
        "last_reauth_timestamp": datetime.now(timezone.utc).isoformat(),
        "realized_pnl": 0,
        "unrealized_pnl": 0,
        "broker_health": {
            "connected": True,
            "latency_ms": 45,
            "last_ping": datetime.now(timezone.utc).isoformat(),
        },
        "reconciliation": {
            "matched": True,
            "locked": False,
            "mismatches": [],
        },
        "kill_switch": {
            "status": "ARMED",
            "tripped": False,
            "conditions": [],
        },
        "demotion_history": [],
    }


def generate_risk_state() -> dict:
    """Generate sample risk_kill/state.json."""
    return {
        "status": "ARMED",
        "tripped": False,
        "condition": None,
        "time": None,
        "response": None,
        "triggered_by": None,
        "history": [],
    }


def generate_mlflow_experiments() -> list[dict]:
    """Generate sample MLflow experiment data."""
    experiments = []
    
    strategies = [
        {"name": "Momentum", "id": "HYP-001", "status": "live"},
        {"name": "Mean Reversion", "id": "HYP-002", "status": "paper"},
        {"name": "Breakout", "id": "HYP-003", "status": "rejected"},
        {"name": "Trend Following", "id": "HYP-004", "status": "live"},
    ]
    
    for strategy in strategies:
        experiments.append({
            "hypothesis_id": strategy["id"],
            "strategy": strategy["name"],
            "status": strategy["status"],
            "date_range": "2020-01-01 to 2026-01-01",
            "metrics": {
                "sharpe": 1.2 if strategy["status"] == "live" else 0.8,
                "sharpe_cost_adjusted": 0.9 if strategy["status"] == "live" else 0.3,
                "total_return": 0.45 if strategy["status"] == "live" else 0.15,
                "max_drawdown": 0.12 if strategy["status"] == "live" else 0.25,
                "turnover": 0.8,
            },
            "validation": {
                "deflated_sharpe": {
                    "probability": 0.95 if strategy["status"] == "live" else 0.3,
                },
                "cpcv_pass": strategy["status"] == "live",
            },
            "gate_result": {
                "verdict": "pass" if strategy["status"] == "live" else "fail",
                "score": 0.95 if strategy["status"] == "live" else 0.4,
                "checks": [
                    {"name": "beats_all_baselines", "status": "pass" if strategy["status"] == "live" else "fail"},
                    {"name": "deflated_sharpe_positive", "status": "pass" if strategy["status"] == "live" else "fail"},
                    {"name": "cpcv_pass", "status": "pass" if strategy["status"] == "live" else "fail"},
                    {"name": "survives_pessimistic_costs", "status": "pass" if strategy["status"] == "live" else "fail"},
                    {"name": "live_paper_divergence_ok", "status": "pass" if strategy["status"] == "live" else "fail"},
                ],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
            "variants_tested": 50,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "run_id": f"run_{strategy['id']}",
        })
    
    return experiments


def generate_reconciliation_data() -> dict:
    """Generate sample reconciliation data."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "matched": True,
        "locked": False,
        "mismatches": [],
        "positions": {
            "RELIANCE": {"expected": 100, "actual": 100},
            "TCS": {"expected": 50, "actual": 50},
            "INFY": {"expected": 75, "actual": 75},
        },
    }


def generate_execution_log() -> list[dict]:
    """Generate sample execution log."""
    return [
        {
            "internal_order_id": "ORD-001",
            "idempotency_key": "ik_001",
            "broker_order_id": "BRK-001",
            "symbol": "RELIANCE",
            "side": "BUY",
            "quantity": 100,
            "price": 2500.0,
            "status": "FILLED",
            "filled_quantity": 100,
            "expected_price": 2500.0,
            "actual_price": 2500.5,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "filled_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "internal_order_id": "ORD-002",
            "idempotency_key": "ik_002",
            "broker_order_id": "BRK-002",
            "symbol": "TCS",
            "side": "BUY",
            "quantity": 50,
            "price": 3500.0,
            "status": "FILLED",
            "filled_quantity": 50,
            "expected_price": 3500.0,
            "actual_price": 3499.5,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "filled_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "internal_order_id": "ORD-003",
            "idempotency_key": "ik_003",
            "broker_order_id": "BRK-003",
            "symbol": "INFY",
            "side": "SELL",
            "quantity": 25,
            "price": 1500.0,
            "status": "REJECTED",
            "filled_quantity": 0,
            "expected_price": 1500.0,
            "actual_price": None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "filled_at": None,
            "reason": "Insufficient margin",
        },
    ]


def generate_alerts() -> list[dict]:
    """Generate sample alerts."""
    return [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "data_quality_warning",
            "message": "Data staleness detected for NIFTY50",
            "severity": "WARNING",
            "details": {"symbol": "NIFTY50", "staleness_days": 1.5},
        },
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "reconciliation_success",
            "message": "EOD reconciliation completed successfully",
            "severity": "INFO",
            "details": {"mismatches": 0},
        },
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "pipeline_completed",
            "message": "Daily pipeline run completed",
            "severity": "INFO",
            "details": {"status": "success", "duration_seconds": 125},
        },
    ]


def generate_diagnostics() -> dict:
    """Generate sample diagnostic results (HYP-DIAG-001)."""
    return {
        "hypothesis_id": "HYP-DIAG-001",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall_status": "passed",
        "summary": "All diagnostic checks passed. Harness is working correctly.",
        "results": [
            {
                "step": "buy_and_hold_sanity",
                "status": "passed",
                "message": "Buy-and-hold sanity check passed",
                "details": {
                    "cagr": 0.12,
                    "max_drawdown": 0.15,
                    "test_window": ["2020-01-01", "2026-01-01"],
                },
            },
            {
                "step": "zero_cost_pass",
                "status": "passed",
                "message": "5 strategies show edge with zero costs",
                "details": {
                    "strategies_with_edge": 5,
                    "strategies_checked": 20,
                },
            },
            {
                "step": "look_ahead_check",
                "status": "passed",
                "message": "No look-ahead bias detected",
                "details": {
                    "strategies_checked": 20,
                },
            },
            {
                "step": "universe_check",
                "status": "passed",
                "message": "Universe check passed",
                "details": {
                    "delisted_stocks": 15,
                    "current_stocks": 50,
                },
            },
            {
                "step": "cost_breakdown",
                "status": "passed",
                "message": "12 strategies with positive gross P&L, 8 killed by costs",
                "details": {
                    "strategies_with_positive_gross": 12,
                    "strategies_killed_by_costs": 8,
                },
            },
        ],
    }


def generate_capital_ladder() -> dict:
    """Generate sample capital ladder configuration."""
    return {
        "stages": [
            {"name": "Paper Trading", "amount": 0, "currency": "₹"},
            {"name": "Stage 1", "amount": 10000, "currency": "₹"},
            {"name": "Stage 2", "amount": 50000, "currency": "₹"},
            {"name": "Stage 3", "amount": 100000, "currency": "₹"},
        ],
        "current_stage": 1,  # Paper Trading is stage 0, Stage 1 is index 1
        "promotion_criteria": [
            "beats_all_baselines",
            "deflated_sharpe_positive",
            "cpcv_pass",
            "survives_pessimistic_costs",
            "live_paper_divergence_ok",
            "min_paper_trading_days",
        ],
    }


def main() -> None:
    """Generate all sample data files."""
    print("Generating sample data for Quant India Dashboard...")
    print()
    
    # Create directories
    dirs = [
        "var/diagnostics",
        "var",
        "risk_kill",
        "reports/generated/experiments",
        "reconciliation",
        "execution",
        "observability",
        "config",
    ]
    
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
    
    # Generate and save files
    files = {
        "observability/health_check.json": generate_health_check(),
        "risk_kill/state.json": generate_risk_state(),
        "reports/generated/experiments/experiments.jsonl": generate_mlflow_experiments(),
        "reconciliation/results.json": generate_reconciliation_data(),
        "execution/orders.jsonl": generate_execution_log(),
        "observability/alerts.jsonl": generate_alerts(),
        "var/diagnostics/HYP-DIAG-001.json": generate_diagnostics(),
        "config/capital_ladder.json": generate_capital_ladder(),
    }
    
    for path, data in files.items():
        if path.endswith('.jsonl'):
            # Write as JSONL
            with open(path, 'w') as f:
                for item in data:
                    f.write(json.dumps(item) + '\n')
        else:
            # Write as JSON
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        
        print(f"✓ Created: {path}")
    
    print()
    print("Sample data generated successfully!")
    print()
    print("You can now test the dashboard with:")
    print("    streamlit run dashboard/main_dashboard.py")


if __name__ == "__main__":
    main()
