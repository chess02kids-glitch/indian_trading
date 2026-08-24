"""Streamlit dashboard foundation for the paper-trading system.

Functionality first, visuals later. Run with:

    streamlit run dashboard/paper_dashboard.py

Reads the operational status document written by the orchestration
pipeline (default ``var/operational_status.json``, override with
``QUANT_INDIA_PAPER_STATUS``) and the latest research summary. Missing
files are shown as "unknown" — the dashboard never assumes a healthy
system.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_STATUS_FILE = "var/operational_status.json"
DEFAULT_SUMMARY_FILE = "reports/generated/baseline_experiment_summary.json"

_HEALTH_COLORS = {
    "HEALTHY": "green",
    "WARNING": "orange",
    "HALTED": "red",
    "LOCKED": "red",
    "unknown": "gray",
}


def load_json(path: str | Path) -> dict[str, Any] | None:
    """Load a JSON document, returning None when missing or malformed."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def status_file_path(environ: dict[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    return Path(environment.get("QUANT_INDIA_PAPER_STATUS", DEFAULT_STATUS_FILE))


def summarize_status(status: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce the operational document to dashboard fields."""
    if not status:
        return {
            "health": "unknown",
            "risk_state": "unknown",
            "reconciliation": "unknown",
            "latest_run": "unknown",
            "positions": [],
            "open_orders": [],
            "alerts": [],
        }
    reconciliation = status.get("reconciliation")
    reconciliation_text = (
        ("matched" if reconciliation.get("matched") else "MISMATCH")
        if isinstance(reconciliation, dict)
        else str(reconciliation)
    )
    return {
        "health": str(status.get("system_health", "unknown")),
        "risk_state": str(status.get("risk_state", "unknown")),
        "reconciliation": reconciliation_text,
        "latest_run": str(status.get("latest_run", "unknown")),
        "paper_cash": status.get("paper_cash"),
        "positions": list(status.get("paper_positions", [])),
        "open_orders": list(status.get("open_orders", [])),
        "alerts": list(status.get("alerts_recent", [])),
    }


def render(status_path: str | Path, summary_path: str | Path) -> None:
    """Render the dashboard (called by ``streamlit run``)."""
    import streamlit as st

    st.set_page_config(page_title="Quant India — Paper", layout="wide")
    st.title("Quant India — Paper Trading")

    status = load_json(status_path)
    summary = load_json(summary_path)
    view = summarize_status(status)

    st.subheader("System health")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Health", view["health"])
    col2.metric("Risk state", view["risk_state"])
    col3.metric("Reconciliation", view["reconciliation"])
    col4.metric("Latest run", view["latest_run"])

    if view["health"] in ("HALTED", "LOCKED", "unknown"):
        st.error(
            "System is not in a healthy state. Unknown values require operator "
            "investigation — do not assume trading is safe."
        )

    st.subheader("Latest research run")
    if summary:
        st.json(
            {
                "strategy": summary.get("strategy"),
                "status": summary.get("status"),
                "reason": summary.get("reason"),
                "cost_model_scenario": (summary.get("cost_model") or {}).get(
                    "scenario"
                ),
                "oos_period": summary.get("oos_period"),
                "deflated_sharpe": summary.get("deflated_sharpe"),
            }
        )
        metrics = summary.get("full_period_metrics") or {}
        if metrics:
            st.dataframe(metrics)
    else:
        st.info("No research summary found yet.")

    st.subheader("Paper positions")
    if view["positions"]:
        st.dataframe(view["positions"])
        if view.get("paper_cash") is not None:
            st.caption(f"Paper cash: {view['paper_cash']:.2f}")
    else:
        st.info("No paper positions.")

    st.subheader("Open paper orders")
    if view["open_orders"]:
        st.dataframe(view["open_orders"])
    else:
        st.info("No open paper orders.")

    st.subheader("Latest alerts")
    if view["alerts"]:
        for alert in view["alerts"]:
            level = alert.get("severity", "INFO")
            writer = (
                st.info
                if level == "INFO"
                else (st.warning if level == "WARNING" else st.error)
            )
            writer(f"{level} — {alert.get('event')}: {alert.get('message')}")
    else:
        st.info("No alerts recorded.")


def main() -> None:
    render(status_file_path(), DEFAULT_SUMMARY_FILE)


if __name__ == "__main__":
    main()
