"""Tests for the read-only RC-1 operational dashboard."""

import json
from pathlib import Path

from dashboard.operational import collect_status, status_file_path
from dashboard.server import render_dashboard


def test_status_file_path_uses_configured_environment():
    assert status_file_path({"QUANT_INDIA_STATUS_FILE": "/tmp/status.json"}) == Path(
        "/tmp/status.json"
    )


def test_collect_status_returns_unknown_when_status_file_is_missing(tmp_path):
    status = collect_status({"QUANT_INDIA_STATUS_FILE": str(tmp_path / "missing.json")})
    assert status["status_file"] == "unavailable"
    assert status["broker_health"] == "unknown"


def test_collect_status_loads_expected_operational_fields(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"broker_health": "healthy", "open_orders": 0}))
    status = collect_status({"QUANT_INDIA_STATUS_FILE": str(path)})
    assert status["status_file"] == "loaded"
    assert status["broker_health"] == "healthy"
    assert status["kill_switch"] == "unknown"


def test_render_dashboard_escapes_status_values():
    rendered = render_dashboard(
        {
            "generated_at": "now",
            "status_file": "loaded",
            "broker_health": "<bad>",
            "reconciliation": "ok",
            "kill_switch": "on",
            "latest_experiment": "x",
            "open_orders": 0,
            "system_health": "ok",
        }
    )
    assert b"&lt;bad&gt;" in rendered
