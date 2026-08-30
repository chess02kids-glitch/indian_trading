"""Quant India Dashboard Module.

This module contains all dashboard components for the Quant India trading system.

Main Dashboard (v1):
    - dashboard/main_dashboard.py - Primary Streamlit dashboard with all 6 screens

Legacy Dashboards:
    - dashboard/research_dashboard.py - Read-only research dashboard
    - dashboard/paper_dashboard.py - Paper trading dashboard
    - dashboard/broker_dashboard.py - Broker status dashboard
    - dashboard/operational.py - Operational status collection
    - dashboard/server.py - HTTP server for dashboards
    - dashboard/cockpit_html.py - HTML cockpit rendering
    - dashboard/research_api.py - Research API endpoints

Run the main dashboard with:
    streamlit run dashboard/main_dashboard.py

Architecture:
    The dashboard is read-mostly. It reads from:
    - health_check.json (observability/)
    - MLflow (research ledger, DSR, gate status)
    - risk_kill/state (kill-switch status, active halts)
    - execution/ (order log, fill status)
    - reconciliation/ (EOD diffs, mismatch flags)

    The dashboard writes only to:
    - pause/resume commands
    - promote/demote confirmation
    - manual kill switch
"""


def main() -> None:
    """Entry point for the Streamlit main dashboard (lazy import).

    Imported lazily so the stdlib HTTP server (``dashboard.server``) keeps
    working in environments without Streamlit installed.
    """
    from .main_dashboard import main as _main

    _main()


__all__ = ["main"]
