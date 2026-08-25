"""Tests for the research dashboard helpers (portfolio, history, exposure)."""

from __future__ import annotations

import json

from dashboard.paper_dashboard import (
    load_experiment_history,
    summarize_period_report,
    summarize_report,
)


def test_load_experiment_history_reads_jsonl(tmp_path) -> None:
    path = tmp_path / "experiments.jsonl"
    path.write_text(
        json.dumps({"hypothesis_id": "HYP-00001", "status": "rejected"}) + "\n"
        + "not json\n"
        + json.dumps({"hypothesis_id": "HYP-00002", "status": "accepted"}) + "\n"
    )
    records = load_experiment_history(path)
    assert [r["hypothesis_id"] for r in records] == ["HYP-00001", "HYP-00002"]
    assert records[0]["status"] == "rejected"


def test_load_experiment_history_missing_returns_empty(tmp_path) -> None:
    assert load_experiment_history(tmp_path / "missing.jsonl") == []


def test_summarize_report_reduces_metrics_and_weights() -> None:
    view = summarize_report(
        {
            "metrics": {"sharpe": 1.2, "total_return": 0.5},
            "allocation_summary": {"final_weights": {"A": 0.6}, "turnover_total": 3.0},
            "drawdowns": [{"value": -0.1}],
        }
    )
    assert view["metrics"]["sharpe"] == 1.2
    assert view["final_weights"] == {"A": 0.6}
    assert len(view["drawdowns"]) == 1


def test_summarize_report_empty() -> None:
    assert summarize_report(None) == {}


def test_summarize_period_report_last_period() -> None:
    view = summarize_period_report(
        {"periods": [{"period_start": "2024-01-01", "factor_exposure": 0.4}]}
    )
    assert view["period_count"] == 1
    assert view["last_period"]["factor_exposure"] == 0.4


def test_summarize_period_report_empty() -> None:
    assert summarize_period_report(None) == {}
