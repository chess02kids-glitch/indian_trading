"""Broker health status collection and read-only dashboard data paths."""

from __future__ import annotations

import json

from broker.status import (
    BROKER_STATUS_FIELDS,
    collect_broker_health,
    load_prior_status,
    merge_recent_orders,
    order_entry,
    summarize_reconciliation,
    write_broker_status,
)
from dashboard.broker_dashboard import render_sections
from dashboard.broker_status import (
    collect_broker_dashboard_status,
    summarize_broker_health,
)
from tests.sandbox_common import SandboxEnv, make_intent


def _status_file(tmp_path, monkeypatch):
    path = tmp_path / "var" / "broker_status.json"
    monkeypatch.setenv("QUANT_BROKER_STATUS_FILE", str(path))
    return path


class TestStatusCollection:
    def test_healthy_when_connected_and_authed(self, tmp_path, monkeypatch) -> None:
        env = SandboxEnv(tmp_path / "u", "upstox")
        env.login()
        _status_file(tmp_path, monkeypatch)
        document = collect_broker_health({"upstox": env.adapter})
        assert document["sandbox_health"] == "healthy"
        assert document["broker_connectivity"] == {"upstox": "connected"}
        assert document["token_status"]["upstox"]["state"] == "active"

    def test_degraded_when_token_missing(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path / "u", "upstox")
        document = collect_broker_health({"upstox": env.adapter})
        assert document["sandbox_health"] == "degraded"
        assert document["token_status"]["upstox"]["state"] == "missing"

    def test_unknown_without_adapters(self) -> None:
        document = collect_broker_health({})
        assert document["sandbox_health"] == "unknown"
        assert document["broker_connectivity"] == "unknown"
        assert document["token_status"] == "unknown"

    def test_unreachable_broker_degrades(self, tmp_path) -> None:
        from broker.simulated import TimeoutFault

        env = SandboxEnv(tmp_path / "u", "upstox")
        env.login()
        env.transport.script("ping", [TimeoutFault()])
        document = collect_broker_health({"upstox": env.adapter})
        assert document["broker_connectivity"]["upstox"] == "unreachable"
        assert document["sandbox_health"] == "degraded"

    def test_masking_never_leaks_token(self, tmp_path, monkeypatch) -> None:
        env = SandboxEnv(tmp_path / "u", "upstox")
        token = env.login()
        _status_file(tmp_path, monkeypatch)
        document = collect_broker_health({"upstox": env.adapter})
        assert token not in json.dumps(document)


class TestRecentOrdersMerge:
    def test_merge_dedups_and_caps(self) -> None:
        existing = [
            {"internal_order_id": f"o{i}", "status": "FILLED"} for i in range(30)
        ]
        merged = merge_recent_orders(
            existing, [{"internal_order_id": "new", "status": "FILLED"}]
        )
        assert len(merged) == 25
        assert merged[-1]["internal_order_id"] == "new"

    def test_order_entry_serialises_result(self, tmp_path) -> None:
        env = SandboxEnv(tmp_path / "u", "upstox")
        env.login()
        record = env.adapter.place_limit_order(make_intent("ord-1"))
        from broker.reconciler import record_to_result

        entry = order_entry(record_to_result(record))
        assert entry["status"] == "FILLED"
        assert entry["symbol"] == "RELIANCE"
        assert entry["internal_order_id"] == "ord-1"


class TestStatusDocumentPersistence:
    def test_write_and_load_prior(self, tmp_path, monkeypatch) -> None:
        path = _status_file(tmp_path, monkeypatch)
        write_broker_status({"sandbox_health": "healthy"})
        prior = load_prior_status()
        assert prior["sandbox_health"] == "healthy"
        assert json.loads(path.read_text())["sandbox_health"] == "healthy"

    def test_load_prior_tolerates_garbage(self, tmp_path, monkeypatch) -> None:
        path = _status_file(tmp_path, monkeypatch)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json")
        assert load_prior_status() == {}


class TestDashboardData:
    def test_missing_file_reports_unknown(self, tmp_path, monkeypatch) -> None:
        _status_file(tmp_path, monkeypatch)
        snapshot = collect_broker_dashboard_status()
        assert snapshot["status_file"] == "unavailable"
        for field in BROKER_STATUS_FIELDS:
            assert snapshot[field] == "unknown"
        summary = summarize_broker_health(snapshot)
        assert summary["overall"] == "unknown"
        assert summary["recent_orders"] == []

    def test_loaded_document_flows_through(self, tmp_path, monkeypatch) -> None:
        path = _status_file(tmp_path, monkeypatch)
        document = {
            "generated_at": "2026-08-25T10:00:00+00:00",
            "broker_connectivity": {"upstox": "connected"},
            "token_status": {
                "upstox": {
                    "state": "active",
                    "refresh_due": False,
                    "masked_token": "abc…xyz",
                }
            },
            "sandbox_health": "healthy",
            "reconciliation_health": {"state": "matched", "locked": False},
            "recent_sandbox_orders": [
                {"internal_order_id": "o1", "symbol": "RELIANCE", "status": "FILLED"}
            ],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        snapshot = collect_broker_dashboard_status()
        assert snapshot["status_file"] == "loaded"
        summary = summarize_broker_health(snapshot)
        assert summary["overall"] == "healthy"
        assert summary["connectivity"]["upstox"] == "connected"
        assert summary["tokens"]["upstox"]["state"] == "active"
        assert summary["reconciliation"]["state"] == "matched"
        assert len(summary["recent_orders"]) == 1

    def test_malformed_document_reports_error(self, tmp_path, monkeypatch) -> None:
        path = _status_file(tmp_path, monkeypatch)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1, 2, 3]")
        snapshot = collect_broker_dashboard_status()
        assert snapshot["status_file"] == "unavailable"
        assert "status_error" in snapshot

    def test_render_sections_has_all_views(self) -> None:
        summary = {
            "overall": "healthy",
            "connectivity": {"upstox": "connected"},
            "tokens": {"upstox": {"state": "active", "refresh_due": False}},
            "sandbox_health": "healthy",
            "reconciliation": {"state": "locked", "mismatches": 2},
            "recent_orders": [{"internal_order_id": "o1", "status": "FILLED"}],
        }
        sections = render_sections(summary)
        titles = [section["title"] for section in sections]
        assert titles == [
            "Broker Connectivity",
            "Token Status",
            "Sandbox Health",
            "Reconciliation Health",
            "Recent Sandbox Orders",
        ]
        recon = sections[3]["rows"][0]
        assert recon["state"] == "locked"
        assert recon["color"] == "red"

    def test_render_sections_tolerates_empty(self) -> None:
        sections = render_sections({})
        titles = [section["title"] for section in sections]
        assert "Recent Sandbox Orders" in titles
        # dashboard exposes no execution controls — sections are display rows only
        for section in sections:
            assert set(section) == {"title", "rows"}

    def test_summarize_reconciliation_unknown(self) -> None:
        assert summarize_reconciliation(None) == {"state": "unknown"}
