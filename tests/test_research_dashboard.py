"""Tests for the read-only research dashboard helpers."""

from __future__ import annotations

import json

from dashboard.research_dashboard import (
    benchmark_comparison_view,
    factor_diagnostics_view,
    gate_history,
    leaderboard_records,
    load_json,
    load_jsonl,
    timeline_records,
    validation_frame,
)


def _records() -> list[dict]:
    return [
        {
            "hypothesis_id": "HYP-00001",
            "strategy": "momentum",
            "status": "accepted",
            "metrics": {
                "sharpe": 1.2,
                "total_return": 0.4,
                "max_drawdown": -0.1,
                "turnover": 3.0,
            },
            "validation": {"deflated_sharpe": {"probability": 0.97}},
            "started_at": "2024-01-01T00:00:00",
            "ended_at": "2024-01-01T00:00:01",
            "gate_result": {
                "verdict": "PASS",
                "score": 90.0,
                "checks": [{"name": "dsr", "status": "pass", "message": "ok"}],
                "generated_at": "2024-01-01T00:00:02",
            },
        },
        {
            "hypothesis_id": "HYP-00002",
            "strategy": "meanrev",
            "status": "failed",
            "metrics": {"sharpe": -0.5},
            "started_at": "2024-01-02T00:00:00",
            "ended_at": "2024-01-02T00:00:01",
            "gate_result": {
                "verdict": "FAIL",
                "score": 10.0,
                "checks": [{"name": "dsr", "status": "fail", "message": "no"}],
            },
        },
    ]


def test_load_json_and_jsonl(tmp_path) -> None:
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"a": 1}))
    assert load_json(path) == {"a": 1}
    assert load_json(tmp_path / "missing.json") is None

    records_path = tmp_path / "records.jsonl"
    records_path.write_text(
        json.dumps({"id": 1}) + "\n" + "broken\n" + json.dumps({"id": 2}) + "\n"
    )
    assert [row["id"] for row in load_jsonl(records_path)] == [1, 2]


def test_leaderboard_orders_by_deflated_sharpe_and_keeps_losers() -> None:
    rows = leaderboard_records(_records())
    assert [row["hypothesis_id"] for row in rows] == ["HYP-00001", "HYP-00002"]
    assert rows[1]["status"] == "failed"
    assert 0.97 in rows[0].values()


def test_gate_history_flattens_verdicts() -> None:
    history = gate_history(_records())
    assert [row["verdict"] for row in history] == ["PASS", "FAIL"]
    assert history[0]["generated_at"] == "2024-01-01T00:00:02"
    assert history[1]["check_failures"] == ["dsr"]


def test_gate_history_missing_gate_is_omitted() -> None:
    assert gate_history([{"hypothesis_id": "HYP-1", "status": "running"}]) == []


def test_validation_frame_reduces_fold_metrics() -> None:
    frames = validation_frame(
        {
            "fold_metrics": [{"sharpe": 0.5, "observations": 10, "total_return": 0.1}],
            "windows": [{"test_start": "2024-01-01", "test_end": "2024-01-10"}],
        }
    )
    assert len(frames) == 1
    assert frames[0]["sharpe"] == 0.5
    assert frames[0]["test_start"] == "2024-01-01"


def test_validation_frame_empty_when_missing() -> None:
    assert validation_frame(None) == []
    assert validation_frame({}) == []


def test_factor_diagnostics_view() -> None:
    view = factor_diagnostics_view(
        {"factor_decay": {"m": {"1": 0.2}}, "rank_stability": {"m": 0.9}}
    )
    assert view["factor_decay"]["m"]["1"] == 0.2
    assert view["rank_stability"]["m"] == 0.9
    assert factor_diagnostics_view(None) == {}


def test_benchmark_comparison_sorted() -> None:
    rows = benchmark_comparison_view(
        {
            "benchmark_comparison": {
                "buy_and_hold": {"sharpe": 0.2, "max_drawdown": -0.2},
                "equal_weight": {"sharpe": 0.9, "max_drawdown": -0.1},
            }
        }
    )
    assert [row["name"] for row in rows] == ["buy_and_hold", "equal_weight"]
    assert rows[1]["sharpe"] == 0.9
    assert benchmark_comparison_view(None) == []


def test_timeline_is_oldest_first() -> None:
    rows = timeline_records(_records())
    assert [row["hypothesis_id"] for row in rows] == ["HYP-00001", "HYP-00002"]
    assert rows[0]["started_at"] == "2024-01-01T00:00:00"
