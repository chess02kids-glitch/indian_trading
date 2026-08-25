"""Read-only Streamlit broker health dashboard.

Views (all strictly read-only — there are no execution controls of any
kind; the dashboard cannot place, amend, or cancel orders):

* Broker Connectivity — sandbox connectivity per broker
* Token Status — token expiry tracking and re-auth reminders
* Sandbox Health — aggregate health of the sandbox environment
* Reconciliation Health — latest reconciliation outcome / lock state
* Recent Sandbox Orders — the most recent order records (display-only)

The dashboard renders the status document produced by the broker CLI /
jobs (default ``var/broker_status.json``, override with
``QUANT_BROKER_STATUS_FILE``). Missing data renders as "unknown".

Run with::

    streamlit run dashboard/broker_dashboard.py
"""

from __future__ import annotations

from typing import Any

from dashboard.broker_status import (
    collect_broker_dashboard_status,
    summarize_broker_health,
)

_HEALTH_COLORS = {
    "healthy": "green",
    "connected": "green",
    "active": "green",
    "degraded": "orange",
    "expiring_soon": "orange",
    "unreachable": "red",
    "expired": "red",
    "missing": "red",
    "locked": "red",
    "error": "red",
    "unknown": "gray",
}


def _color(state: str) -> str:
    return _HEALTH_COLORS.get(state.lower(), "gray")


def render_sections(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the renderable section model (UI-agnostic, fully testable)."""
    sections: list[dict[str, Any]] = []

    connectivity_rows = [
        {
            "broker": broker,
            "state": state,
            "color": _color(str(state)),
        }
        for broker, state in summary.get("connectivity", {}).items()
    ]
    sections.append(
        {
            "title": "Broker Connectivity",
            "rows": connectivity_rows
            or [{"broker": "-", "state": "unknown", "color": "gray"}],
        }
    )

    token_rows = [
        {
            "broker": broker,
            "state": str(info.get("state", "unknown")),
            "expires_in_seconds": info.get("expires_in_seconds"),
            "refresh_due": bool(info.get("refresh_due")),
            "masked_token": info.get("masked_token"),
            "color": _color(str(info.get("state", "unknown"))),
        }
        for broker, info in summary.get("tokens", {}).items()
        if isinstance(info, dict)
    ]
    sections.append(
        {
            "title": "Token Status",
            "rows": token_rows
            or [{"broker": "-", "state": "unknown", "color": "gray"}],
        }
    )

    sandbox_state = str(summary.get("sandbox_health", "unknown"))
    sections.append(
        {
            "title": "Sandbox Health",
            "rows": [{"state": sandbox_state, "color": _color(sandbox_state)}],
        }
    )

    reconciliation = summary.get("reconciliation", "unknown")
    if isinstance(reconciliation, dict):
        recon_rows = [
            {
                "state": reconciliation.get("state", "unknown"),
                "mismatches": reconciliation.get("mismatches"),
                "as_of": reconciliation.get("as_of"),
                "color": _color(str(reconciliation.get("state", "unknown"))),
            }
        ]
    else:
        recon_rows = [
            {"state": str(reconciliation), "color": _color(str(reconciliation))}
        ]
    sections.append({"title": "Reconciliation Health", "rows": recon_rows})

    order_rows = [
        {
            "internal_order_id": order.get("internal_order_id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "status": order.get("status"),
            "requested_quantity": order.get("requested_quantity"),
            "filled_quantity": order.get("filled_quantity"),
            "average_fill_price": order.get("average_fill_price"),
            "reason": order.get("reason"),
            "timestamp": order.get("timestamp"),
        }
        for order in summary.get("recent_orders", [])
        if isinstance(order, dict)
    ]
    sections.append(
        {
            "title": "Recent Sandbox Orders",
            "rows": order_rows or [{"status": "none recorded"}],
        }
    )
    return sections


def render() -> None:
    """Render the dashboard (called by ``streamlit run``).

    Read-only by construction: no callbacks mutate state. Streamlit is
    imported lazily so this module is importable without the dependency.
    """
    import streamlit as st

    st.set_page_config(page_title="Quant India — Broker Health", layout="wide")
    st.title("Broker Health — Sandbox (read-only)")
    st.caption(
        "Sandbox broker operations status. This dashboard never places, "
        "amends, or cancels orders; unknown values require operator review."
    )

    snapshot = collect_broker_dashboard_status()
    summary = summarize_broker_health(snapshot)
    for section in render_sections(summary):
        st.subheader(section["title"])
        st.json(section["rows"])

    st.caption(
        f"Snapshot: {summary.get('source', 'n/a')} · generated: "
        f"{summary.get('generated_at', 'unknown')}"
    )


if __name__ == "__main__":  # pragma: no cover - streamlit entry point
    render()
