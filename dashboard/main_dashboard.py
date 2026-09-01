#!/usr/bin/env python
"""Quant India - Main Dashboard & Full Automation Control Plane (v1).

This is the primary Streamlit dashboard implementing all 6 screens from
Section 2 of the specification:

Screen 1 - Command Center (the one you check every morning)
Screen 2 - Research Pipeline
Screen 3 - Risk & Kill Switch
Screen 4 - Capital Ladder
Screen 5 - Reconciliation & Broker Health
Screen 6 - Alerts & Incident Log

Architecture:
- Reads-only from: health_check.json, MLflow, risk_kill/state, execution/, reconciliation/
- Writes only to: pause/resume, promote/demote confirmation, manual kill switch

Run with:
    streamlit run dashboard/main_dashboard.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import streamlit as st

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_COLORS = {
    "RESEARCH": "blue",
    "PAPER": "orange",
    "LIVE": "green",
    "HALTED": "red",
    "LOCKED": "red",
    "HEALTHY": "green",
    "WARNING": "orange",
    "unknown": "gray",
}

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def load_health_check() -> dict[str, Any]:
    """Load the health check JSON file."""
    health_paths = [
        "observability/health_check.json",
        "var/operational_status.json",
        "var/health_check.json",
    ]
    for path in health_paths:
        if Path(path).exists():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return {"state": "unknown", "reason": "health check file not found"}


def load_risk_state() -> dict[str, Any]:
    """Load the risk kill switch state."""
    risk_paths = [
        "risk_kill/state.json",
        "var/risk_state.json",
    ]
    for path in risk_paths:
        if Path(path).exists():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return {"status": "ARMED", "tripped": False, "conditions": []}


def load_mlflow_data() -> list[dict[str, Any]]:
    """Load MLflow experiment data."""
    mlflow_paths = [
        "reports/generated/experiments/experiments.jsonl",
        "mlruns/0/meta.yaml",  # MLflow default
    ]

    # Try JSONL first
    for path in mlflow_paths:
        if Path(path).exists():
            try:
                if path.endswith(".jsonl"):
                    records = []
                    for line in Path(path).read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            records.append(json.loads(line))
                    return records
                else:
                    return [json.loads(Path(path).read_text(encoding="utf-8"))]
            except (OSError, ValueError):
                continue
    return []


def load_reconciliation_data() -> dict[str, Any]:
    """Load reconciliation data."""
    recon_paths = [
        "reconciliation/results.json",
        "var/reconciliation.json",
    ]
    for path in recon_paths:
        if Path(path).exists():
            try:
                return json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
    return {}


def load_execution_log() -> list[dict[str, Any]]:
    """Load execution order log."""
    exec_paths = [
        "execution/orders.jsonl",
        "var/execution_log.jsonl",
    ]
    for path in exec_paths:
        if Path(path).exists():
            try:
                records = []
                for line in Path(path).read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        records.append(json.loads(line))
                return records
            except (OSError, ValueError):
                continue
    return []


def load_alerts() -> list[dict[str, Any]]:
    """Load Telegram alerts."""
    alert_paths = [
        "observability/alerts.jsonl",
        "var/alerts.jsonl",
    ]
    for path in alert_paths:
        if Path(path).exists():
            try:
                records = []
                for line in Path(path).read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        records.append(json.loads(line))
                return records
            except (OSError, ValueError):
                continue
    return []


def load_diagnostics() -> dict[str, Any] | None:
    """Load diagnostic protocol results."""
    diag_path = "var/diagnostics/HYP-DIAG-001.json"
    if Path(diag_path).exists():
        try:
            return json.loads(Path(diag_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return None


def load_capital_ladder() -> dict[str, Any]:
    """Load capital ladder configuration."""
    ladder_path = "config/capital_ladder.json"
    if Path(ladder_path).exists():
        try:
            return json.loads(Path(ladder_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    # Default capital ladder stages
    return {
        "stages": [
            {"name": "Paper Trading", "amount": 0, "currency": "₹"},
            {"name": "Stage 1", "amount": 10000, "currency": "₹"},
            {"name": "Stage 2", "amount": 50000, "currency": "₹"},
            {"name": "Stage 3", "amount": 100000, "currency": "₹"},
        ],
        "current_stage": 0,
        "promotion_criteria": [
            "beats_all_baselines",
            "deflated_sharpe_positive",
            "cpcv_pass",
            "survives_pessimistic_costs",
            "live_paper_divergence_ok",
            "min_paper_trading_days",
        ],
    }


# ---------------------------------------------------------------------------
# Go-Live Gate Implementation (Section 4)
# ---------------------------------------------------------------------------


def live_trading_unlockable(strategy_id: str | None = None) -> tuple[bool, list[str]]:
    """Check if live trading can be unlocked for a strategy.

    This implements the go-live gate from Section 4 of the specification.
    All checks must be green before the toggle becomes clickable.
    """
    checks = {
        "beats_all_baselines": False,
        "deflated_sharpe_positive": False,
        "cpcv_pass": False,  # nosec B105 (gate-check name, not a credential)
        "survives_pessimistic_costs": False,
        "live_paper_divergence_ok": False,
        "min_paper_trading_days": False,
    }

    # Load MLflow data to check strategy status
    mlflow_data = load_mlflow_data()

    if strategy_id:
        # Check specific strategy
        for record in mlflow_data:
            if (
                record.get("hypothesis_id") == strategy_id
                or record.get("strategy") == strategy_id
            ):
                gate_result = record.get("gate_result", {})
                if gate_result.get("verdict") == "pass":
                    # All checks passed
                    for check_name in checks:
                        checks[check_name] = True
                    break
                else:
                    # Check individual checks
                    gate_checks = gate_result.get("checks", [])
                    for check in gate_checks:
                        check_name = check.get("name")
                        if check_name in checks:
                            checks[check_name] = check.get("status") == "pass"
    else:
        # Check if any strategy has all checks passed
        for record in mlflow_data:
            gate_result = record.get("gate_result", {})
            if gate_result.get("verdict") == "pass":
                for check_name in checks:
                    checks[check_name] = True
                break

    unlockable = all(checks.values())
    failing = [k for k, v in checks.items() if not v]

    return unlockable, failing


def render_go_live_gate(strategy_id: str | None = None) -> bool:
    """Render the go-live gate UI and return whether live trading is enabled."""
    unlockable, failing = live_trading_unlockable(strategy_id)

    st.subheader("🔐 Go-Live Gate")
    st.markdown("""
    Live trading is **programmatically disabled** until every condition below is met.
    This cannot be overridden manually.
    """)

    col1, col2 = st.columns([2, 1])

    with col1:
        for check_name in [
            "beats_all_baselines",
            "deflated_sharpe_positive",
            "cpcv_pass",
            "survives_pessimistic_costs",
            "live_paper_divergence_ok",
            "min_paper_trading_days",
        ]:
            passed = check_name not in failing
            status_icon = "✅" if passed else "❌"
            status_color = "green" if passed else "red"
            st.markdown(
                f"{status_icon} **{check_name}** :{status_color}[{'PASS' if passed else 'FAIL'}]"
            )

    with col2:
        if unlockable:
            st.success("✅ All checks passed!")

            # Type-to-confirm for safety
            confirmation = st.text_input(
                "Type strategy name to confirm",
                key="live_confirmation",
                placeholder="Enter strategy name",
            )

            if st.button(
                "🚀 Enable Live Trading", disabled=confirmation != strategy_id
            ):
                st.session_state.live_trading_enabled = True
                st.success("Live trading enabled!")
                return True
        else:
            st.error("❌ Cannot enable live trading")
            st.caption(f"Failing checks: {', '.join(failing)}")

    return bool(st.session_state.get("live_trading_enabled", False))


# ---------------------------------------------------------------------------
# Screen 1 - Command Center
# ---------------------------------------------------------------------------


def render_command_center() -> None:
    """Render Screen 1: Command Center."""
    st.header("🎛️ Command Center")
    st.markdown("The one you check every morning")

    health = load_health_check()
    risk_state = load_risk_state()

    # System state banner
    system_state = health.get("state", "unknown")
    state_color = STATE_COLORS.get(system_state, "gray")

    st.markdown(f"### :{state_color}[{system_state}]")

    # Metrics row
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        last_run = health.get("last_run_ts", "unknown")
        st.metric("Last pipeline run", last_run)

    with col2:
        data_age = health.get("data_age_min", "unknown")
        st.metric("Data freshness (min)", data_age)

    with col3:
        broker_ping = health.get("broker_last_ping_min", "unknown")
        st.metric("Broker heartbeat (min)", broker_ping)

    with col4:
        kill_status = risk_state.get("status", "ARMED")
        if risk_state.get("tripped"):
            kill_status = f"TRIPPED: {risk_state.get('condition', 'unknown')}"
        st.metric("Kill switch", kill_status)

    st.markdown("---")

    # Today's P&L (only if live)
    if system_state in ["LIVE", "HEALTHY"]:
        col1, col2 = st.columns(2)
        with col1:
            realized_pnl = health.get("realized_pnl", 0)
            st.metric("Today's Realized P&L", f"₹{realized_pnl:,.2f}")
        with col2:
            unrealized_pnl = health.get("unrealized_pnl", 0)
            st.metric("Today's Unrealized P&L", f"₹{unrealized_pnl:,.2f}")
    else:
        st.info("P&L metrics only available when capital is live")

    # Diagnostic status
    diagnostics = load_diagnostics()
    if diagnostics:
        st.markdown("### 🔍 Diagnostic Status")
        overall = diagnostics.get("overall_status", "unknown")
        overall_color = (
            "green"
            if overall == "passed"
            else "red"
            if overall == "failed"
            else "orange"
        )
        st.markdown(f"**HYP-DIAG-001**: :{overall_color}[{overall}]")

        if overall != "passed":
            st.warning("Diagnostic protocol has not passed. Review before going live.")


# ---------------------------------------------------------------------------
# Screen 2 - Research Pipeline
# ---------------------------------------------------------------------------


def render_research_pipeline() -> None:
    """Render Screen 2: Research Pipeline."""
    st.header("🔬 Research Pipeline")
    st.markdown("All hypotheses tried, not just survivors")

    mlflow_data = load_mlflow_data()

    if not mlflow_data:
        st.warning("No MLflow data found. Run research experiments first.")
        return

    # Strategy table
    st.subheader("Strategy Leaderboard")

    # Prepare data for display
    display_data = []
    for record in mlflow_data:
        display_data.append(
            {
                "ID": record.get("hypothesis_id", record.get("run_id", "unknown")),
                "Strategy": record.get("strategy", "unknown"),
                "Status": record.get("status", "unknown"),
                "Sharpe (raw)": record.get("metrics", {}).get("sharpe", 0),
                "Sharpe (cost-adjusted)": record.get("metrics", {}).get(
                    "sharpe_cost_adjusted", 0
                ),
                "Deflated Sharpe": record.get("validation", {})
                .get("deflated_sharpe", {})
                .get("probability", 0),
                "CPCV": "PASS"
                if record.get("gate_result", {}).get("cpcv_pass")
                else "FAIL",
                "Date Range": record.get("date_range", "unknown"),
            }
        )

    st.dataframe(display_data)

    st.markdown("---")

    # Gate status card
    st.subheader("🎯 Gate Status Card")

    selected_strategy = st.selectbox(
        "Select strategy to view gate status", [d["ID"] for d in display_data]
    )

    # Find the selected strategy
    selected_record = None
    for record in mlflow_data:
        if (
            record.get("hypothesis_id") == selected_strategy
            or record.get("run_id") == selected_strategy
        ):
            selected_record = record
            break

    if selected_record:
        gate_result = selected_record.get("gate_result", {})
        checks = gate_result.get("checks", [])

        col1, col2, col3, col4, col5 = st.columns(5)

        check_names = [
            "beats_all_baselines",
            "deflated_sharpe_positive",
            "cpcv_pass",
            "survives_pessimistic_costs",
            "live_paper_divergence_ok",
        ]

        for idx, check_name in enumerate(check_names):
            with [col1, col2, col3, col4, col5][idx]:
                check = next((c for c in checks if c.get("name") == check_name), None)
                if check:
                    status = check.get("status", "unknown")
                    color = "green" if status == "pass" else "red"
                    st.markdown(f"**{check_name}**")
                    st.markdown(f":{color}[{status.upper()}]")
                else:
                    st.markdown(f"**{check_name}**")
                    st.markdown(":gray[unknown]")

        # Variant count
        variants = selected_record.get("variants_tested", 0)
        st.metric("Variants tested", variants)

        # DSR explanation
        dsr = selected_record.get("validation", {}).get("deflated_sharpe", {})
        if dsr:
            st.markdown("**Deflated Sharpe Ratio**")
            st.json(dsr)

    st.markdown("---")

    # Summary statistics
    col1, col2, col3 = st.columns(3)

    with col1:
        total_strategies = len(mlflow_data)
        st.metric("Total hypotheses", total_strategies)

    with col2:
        passed = sum(1 for r in mlflow_data if r.get("status") == "live")
        st.metric("Live strategies", passed)

    with col3:
        rejected = sum(1 for r in mlflow_data if r.get("status") == "rejected")
        st.metric("Rejected", rejected)


# ---------------------------------------------------------------------------
# Screen 3 - Risk & Kill Switch
# ---------------------------------------------------------------------------


def render_risk_kill_switch() -> None:
    """Render Screen 3: Risk & Kill Switch."""
    st.header("🛡️ Risk & Kill Switch")

    risk_state = load_risk_state()
    health = load_health_check()

    # Risk limits gauges
    st.subheader("Live Risk Gauges")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Daily Loss Limit**")
        daily_loss_used = health.get("daily_loss_used", 0)
        daily_loss_limit = health.get("daily_loss_limit", 10000)
        if daily_loss_limit > 0:
            ratio = daily_loss_used / daily_loss_limit
            st.progress(ratio)
            st.caption(
                f"₹{daily_loss_used:,.2f} / ₹{daily_loss_limit:,.2f} ({ratio * 100:.1f}%)"
            )
        else:
            st.info("No limit configured")

        st.markdown("**Max Drawdown**")
        current_dd = health.get("current_drawdown", 0)
        max_dd_limit = health.get("max_drawdown_limit", 0.2)
        if max_dd_limit > 0:
            ratio = current_dd / max_dd_limit
            st.progress(ratio)
            st.caption(f"{current_dd * 100:.1f}% / {max_dd_limit * 100:.1f}%")

    with col2:
        st.markdown("**Order Rate Limit**")
        orders_this_sec = health.get("orders_this_second", 0)
        rate_cap = health.get("rate_cap", 10)
        if rate_cap > 0:
            ratio = orders_this_sec / rate_cap
            st.progress(ratio)
            st.caption(f"{orders_this_sec} / {rate_cap} orders/sec")
        else:
            st.info("No limit configured")

        st.markdown("**Position Concentration**")
        max_position = health.get("max_position_concentration", 0)
        conc_limit = health.get("concentration_limit", 0.25)
        if conc_limit > 0:
            ratio = max_position / conc_limit
            st.progress(ratio)
            st.caption(f"{max_position * 100:.1f}% / {conc_limit * 100:.1f}%")

    st.markdown("---")

    # Kill switch status
    st.subheader("Kill Switch Status")

    if risk_state.get("tripped"):
        st.error("🚨 **KILL SWITCH TRIPPED**")
        st.markdown(f"**Condition**: {risk_state.get('condition', 'unknown')}")
        st.markdown(f"**Time**: {risk_state.get('time', 'unknown')}")
        st.markdown(f"**Response**: {risk_state.get('response', 'unknown')}")
    else:
        st.success("✅ Kill switch ARMED - no trips")

    st.markdown("---")

    # Trip history
    st.subheader("Kill Switch Trip History")

    trip_history = risk_state.get("history", [])
    if trip_history:
        for trip in trip_history:
            with st.expander(f"🔴 {trip.get('timestamp', 'Unknown time')}"):
                st.json(trip)
    else:
        st.info("No kill switch trips recorded")

    st.markdown("---")

    # Manual kill switch
    st.subheader("⚠️ Manual Kill Switch")
    st.markdown("""
    **Deliberately inconvenient control** - type the stage name to confirm.
    This immediately sets STOP_NEW_ORDERS, always available regardless of system state.
    """)

    stage_name = st.text_input(
        "Type the current stage name to trigger manual kill",
        key="manual_kill_stage",
        placeholder="Enter stage name (e.g., LIVE, PAPER, RESEARCH)",
    )

    current_state = health.get("state", "unknown")

    if st.button("🛑 TRIGGER MANUAL KILL SWITCH", disabled=stage_name != current_state):
        # In a real implementation, this would write to risk_kill/state
        st.error("⚠️ Manual kill switch triggered!")
        st.warning(f"STOP_NEW_ORDERS activated for stage: {current_state}")

        # Write to risk state file
        try:
            Path("risk_kill").mkdir(parents=True, exist_ok=True)
            new_state = {
                "status": "TRIPPED",
                "condition": "manual_override",
                "time": datetime.now(timezone.utc).isoformat(),
                "response": "STOP_NEW_ORDERS",
                "triggered_by": "manual",
                "tripped": True,
            }
            Path("risk_kill/state.json").write_text(json.dumps(new_state, indent=2))
            st.success("Kill switch state written successfully")
        except Exception as e:
            st.error(f"Failed to write kill switch state: {e}")


# ---------------------------------------------------------------------------
# Screen 4 - Capital Ladder
# ---------------------------------------------------------------------------


def render_capital_ladder() -> None:
    """Render Screen 4: Capital Ladder."""
    st.header("🪜 Capital Ladder")
    st.markdown("Visual representation of capital allocation stages")

    ladder_config = load_capital_ladder()
    health = load_health_check()

    stages = ladder_config.get("stages", [])
    current_stage_idx = ladder_config.get("current_stage", 0)

    # Visual ladder
    st.subheader("Current Stage")

    if stages:
        # Display ladder horizontally
        cols = st.columns(len(stages))

        for idx, (col, stage) in enumerate(zip(cols, stages)):
            with col:
                amount = stage.get("amount", 0)
                currency = stage.get("currency", "₹")
                name = stage.get("name", f"Stage {idx + 1}")

                if idx == current_stage_idx:
                    st.markdown(f"**🟢 {name}**")
                    st.markdown(f"**{currency}{amount:,.0f}**")
                elif idx < current_stage_idx:
                    st.markdown(f"~~{name}~~")
                    st.caption(f"{currency}{amount:,.0f}")
                else:
                    st.markdown(f"{name}")
                    st.caption(f"{currency}{amount:,.0f}")
    else:
        st.warning("No capital ladder configuration found")

    st.markdown("---")

    # Go-live gate
    strategy_id = st.selectbox(
        "Select strategy for go-live evaluation",
        [
            d.get("hypothesis_id", d.get("strategy", "unknown"))
            for d in load_mlflow_data()
        ],
    )

    render_go_live_gate(strategy_id if strategy_id else None)

    st.markdown("---")

    # Promotion criteria
    st.subheader("Promotion Criteria Checklist")

    criteria = ladder_config.get("promotion_criteria", [])
    unlockable, failing = live_trading_unlockable(strategy_id if strategy_id else None)

    for criterion in criteria:
        passed = criterion not in failing
        status = "✅" if passed else "❌"
        color = "green" if passed else "red"
        st.markdown(
            f"{status} **{criterion}**: :{color}[{'PASS' if passed else 'FAIL'}]"
        )

    st.markdown("---")

    # Demotion history
    st.subheader("Demotion History")

    demotion_history = health.get("demotion_history", [])
    if demotion_history:
        for demotion in demotion_history:
            with st.expander(f"🔻 {demotion.get('timestamp', 'Unknown')}"):
                st.json(demotion)
    else:
        st.info("No demotions recorded")


# ---------------------------------------------------------------------------
# Screen 5 - Reconciliation & Broker Health
# ---------------------------------------------------------------------------


def render_reconciliation() -> None:
    """Render Screen 5: Reconciliation & Broker Health."""
    st.header("🔄 Reconciliation & Broker Health")

    recon_data = load_reconciliation_data()
    exec_log = load_execution_log()
    health = load_health_check()

    # EOD position diff
    st.subheader("EOD Position Diff")

    if recon_data:
        mismatches = recon_data.get("mismatches", [])
        if mismatches:
            st.error("⚠️ Position mismatches detected!")
            for mismatch in mismatches:
                with st.expander(f"🔴 {mismatch.get('symbol', 'Unknown')}"):
                    expected = mismatch.get("expected", 0)
                    actual = mismatch.get("actual", 0)
                    st.write(f"Expected: {expected}")
                    st.write(f"Actual: {actual}")
                    st.write(f"Difference: {actual - expected}")
        else:
            st.success("✅ All positions match between DuckDB and broker")
    else:
        st.info("No reconciliation data found")

    st.markdown("---")

    # Order-level log
    st.subheader("Order-Level Log")

    if exec_log:
        # Filter options
        status_filter = st.selectbox(
            "Filter by status",
            ["All", "PENDING", "FILLED", "REJECTED", "PARTIAL", "CANCELLED"],
            key="order_status_filter",
        )

        # Filter log
        filtered_log = exec_log
        if status_filter != "All":
            filtered_log = [
                order
                for order in exec_log
                if order.get("status", "").upper() == status_filter
            ]

        st.dataframe(filtered_log)
        st.caption(f"Showing {len(filtered_log)} of {len(exec_log)} orders")
    else:
        st.info("No execution log found")

    st.markdown("---")

    # Token/session status
    st.subheader("Token & Session Status")

    col1, col2 = st.columns(2)

    with col1:
        token_expiry = health.get("token_expiry", "unknown")
        if token_expiry != "unknown":  # nosec B105 (sentinel literal, not a credential)
            expiry_time = (
                datetime.fromisoformat(token_expiry)
                if isinstance(token_expiry, str)
                else token_expiry
            )
            time_remaining = (
                expiry_time - datetime.now(timezone.utc)
            ).total_seconds() / 3600
            st.metric("Token expiry", f"{time_remaining:.1f} hours remaining")
        else:
            st.metric("Token expiry", "unknown")

    with col2:
        last_reauth = health.get("last_reauth_timestamp", "unknown")
        st.metric("Last re-auth", last_reauth)

    st.markdown("---")

    # Broker connectivity
    st.subheader("Broker Connectivity")

    broker_status = health.get("broker_health", {})
    if broker_status:
        st.json(broker_status)
    else:
        st.info("Broker health status not available")


# ---------------------------------------------------------------------------
# Screen 6 - Alerts & Incident Log
# ---------------------------------------------------------------------------


def render_alerts() -> None:
    """Render Screen 6: Alerts & Incident Log."""
    st.header("🚨 Alerts & Incident Log")

    alerts = load_alerts()
    health = load_health_check()

    # Alert history
    st.subheader("Telegram Alert History")

    if alerts:
        # Severity filter
        severity_options = ["All", "INFO", "WARNING", "CRITICAL"]
        severity_filter = st.selectbox("Filter by severity", severity_options)

        # Filter alerts
        filtered_alerts = alerts
        if severity_filter != "All":
            filtered_alerts = [
                alert
                for alert in alerts
                if alert.get("severity", "").upper() == severity_filter
            ]

        # Display alerts
        for alert in filtered_alerts:
            severity = alert.get("severity", "INFO").upper()
            severity_color = {
                "INFO": "blue",
                "WARNING": "orange",
                "CRITICAL": "red",
            }.get(severity, "gray")

            with st.expander(
                f":{severity_color}[{severity}] {alert.get('timestamp', 'Unknown')}"
            ):
                st.markdown(f"**Message**: {alert.get('message', 'No message')}")
                st.markdown(f"**Type**: {alert.get('type', 'unknown')}")
                if alert.get("details"):
                    st.json(alert.get("details"))

        st.caption(f"Showing {len(filtered_alerts)} of {len(alerts)} alerts")
    else:
        st.info("No alerts found")

    st.markdown("---")

    # Incident template
    st.subheader("Incident Template")

    if health.get("state") == "LOCKED":
        st.warning("⚠️ System is LOCKED - incident template auto-populated")

        # Auto-populate incident template
        st.markdown("### Auto-Populated Incident Report")

        incident_text = """
        **What tripped**: {}
        
        **System state at the time**: {}
        
        **Positions at the time**: {}
        
        **Manual actions taken**: {}
        """
        st.markdown(
            incident_text.format(
                health.get("reason", "unknown"),
                health.get("state", "unknown"),
                json.dumps(health.get("positions", {}), indent=2),
                "Pending operator action",
            )
        )

        # Form for manual incident logging
        with st.form("incident_log"):
            incident_title = st.text_input("Incident Title")
            incident_description = st.text_area("Description")
            actions_taken = st.text_area("Actions Taken")
            resolved = st.checkbox("Resolved")

            if st.form_submit_button("Log Incident"):
                incident = {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "title": incident_title,
                    "description": incident_description,
                    "actions_taken": actions_taken,
                    "resolved": resolved,
                }

                # Save incident
                try:
                    Path("var/incidents").mkdir(parents=True, exist_ok=True)
                    incident_path = Path(
                        f"var/incidents/incident_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                    )
                    incident_path.write_text(json.dumps(incident, indent=2))
                    st.success("Incident logged successfully!")
                except Exception as e:
                    st.error(f"Failed to log incident: {e}")
    else:
        st.info("Incident template only auto-populates on LOCK_ACCOUNT events")


# ---------------------------------------------------------------------------
# Main Dashboard
# ---------------------------------------------------------------------------


def main() -> None:
    """Main entry point for the Quant India Dashboard."""
    st.set_page_config(
        page_title="Quant India - Dashboard",
        page_icon="📊",
        layout="wide",
    )

    # Custom CSS (removed due to Python parser issue with 2.5rem)
    # st.markdown(...)  # CSS can be added back when needed

    # Title
    st.markdown(
        '<p class="main-header">Quant India - Dashboard & Control Plane (v1)</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        "**Read-only from everything below** | **Writes only to**: pause/resume, promote/demote, manual kill switch"
    )

    # Navigation
    st.sidebar.title("🎯 Navigation")
    st.sidebar.markdown("Select a screen to view:")

    screen = st.sidebar.radio(
        "Screens",
        [
            "🎛️ Command Center (Screen 1)",
            "🔬 Research Pipeline (Screen 2)",
            "🛡️ Risk & Kill Switch (Screen 3)",
            "🪜 Capital Ladder (Screen 4)",
            "🔄 Reconciliation & Broker Health (Screen 5)",
            "🚨 Alerts & Incident Log (Screen 6)",
            "📊 Strategy Performance (Screen 7)",
        ],
        key="screen_selector",
    )

    st.sidebar.markdown("---")

    # System info in sidebar
    st.sidebar.markdown("### 📋 System Info")
    health = load_health_check()
    system_state = health.get("state", "unknown")
    state_color = STATE_COLORS.get(system_state, "gray")
    st.sidebar.markdown(f"**State**: :{state_color}[{system_state}]")
    st.sidebar.markdown(f"**Last update**: {health.get('updated_at', 'unknown')}")

    # Diagnostic status in sidebar
    diagnostics = load_diagnostics()
    if diagnostics:
        overall = diagnostics.get("overall_status", "unknown")
        overall_color = (
            "green"
            if overall == "passed"
            else "red"
            if overall == "failed"
            else "orange"
        )
        st.sidebar.markdown(f"**Diagnostics**: :{overall_color}[{overall}]")

    # Render selected screen
    st.markdown("---")

    if "Command Center" in screen:
        render_command_center()
    elif "Research Pipeline" in screen:
        render_research_pipeline()
    elif "Risk & Kill Switch" in screen:
        render_risk_kill_switch()
    elif "Capital Ladder" in screen:
        render_capital_ladder()
    elif "Reconciliation" in screen:
        render_reconciliation()
    elif "Alerts" in screen:
        render_alerts()

    # Footer
    st.markdown("---")
    st.caption(
        "Quant India - Dashboard v1 | Read-only operational view | Architecture: Dashboard reads from health_check.json, MLflow, risk_kill/, execution/, reconciliation/"
    )


if __name__ == "__main__":
    main()
