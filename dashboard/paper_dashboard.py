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
DEFAULT_REPORT_FILE = "reports/generated/momentum_quality.json"
DEFAULT_EXPERIMENT_FILE = "reports/generated/experiments/experiments.jsonl"
DEFAULT_PERIOD_FILE = "reports/generated/momentum_quality_report_M.json"

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


def load_experiment_history(path: str | Path) -> list[dict[str, Any]]:
    """Read the JSONL experiment history into a list of records."""
    path = Path(path)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def summarize_report(report: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce a research report to dashboard portfolio fields."""
    if not report:
        return {}
    metrics = report.get("metrics") or {}
    allocation = report.get("allocation_summary") or {}
    return {
        "metrics": metrics,
        "average_weights": allocation.get("average_weights"),
        "final_weights": allocation.get("final_weights"),
        "turnover_total": allocation.get("turnover_total"),
        "drawdowns": report.get("drawdowns"),
    }


def summarize_period_report(period: dict[str, Any] | None) -> dict[str, Any]:
    """Reduce a monthly period report to exposure/factor fields."""
    if not period:
        return {}
    periods = period.get("periods") or []
    last = periods[-1] if periods else {}
    return {
        "last_period": last,
        "period_count": len(periods),
    }


def render(
    status_path: str | Path,
    summary_path: str | Path,
    *,
    report_path: str | Path = DEFAULT_REPORT_FILE,
    experiment_path: str | Path = DEFAULT_EXPERIMENT_FILE,
    period_path: str | Path = DEFAULT_PERIOD_FILE,
) -> None:
    """Render the dashboard (called by ``streamlit run``)."""
    from dashboard.streamlit_guard import require_streamlit as _require_streamlit

    st = _require_streamlit()

    st.set_page_config(page_title="Quant India — Paper", layout="wide")
    st.title("Quant India — Paper Trading")

    status = load_json(status_path)
    summary = load_json(summary_path)
    report = load_json(report_path)
    period = load_json(period_path)
    experiments = load_experiment_history(experiment_path)
    view = summarize_status(status)
    report_view = summarize_report(report)
    period_view = summarize_period_report(period)

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

    st.subheader("Portfolio & risk")
    metrics = report_view.get("metrics") or {}
    if metrics:
        st.dataframe(metrics)
        if report_view.get("drawdowns"):
            st.line_chart(
                {"max_drawdown": [dd["value"] for dd in report_view["drawdowns"]]}
            )
    else:
        st.info("No portfolio metrics available yet.")

    st.subheader("Signals / allocations")
    weights = report_view.get("final_weights")
    if weights:
        st.json(weights)
    else:
        st.info("No allocation summary available yet.")

    st.subheader("Factor exposure (monthly)")
    last_period = period_view.get("last_period")
    if last_period:
        st.json(
            {
                "period_start": last_period.get("period_start"),
                "period_end": last_period.get("period_end"),
                "factor_exposure": last_period.get("factor_exposure"),
                "num_holdings": last_period.get("num_holdings"),
                "exposure": last_period.get("exposure"),
            }
        )
    else:
        st.info("No periodic report found yet.")

    st.subheader("Experiment history")
    if experiments:
        st.dataframe(
            [
                {
                    "hypothesis_id": e.get("hypothesis_id"),
                    "status": e.get("status"),
                    "strategy": e.get("strategy"),
                    "reason": e.get("reason"),
                }
                for e in experiments
            ]
        )
    else:
        st.info("No experiment history found yet.")

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
