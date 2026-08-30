"""Tests for the strategy dashboard (MomReM card, live signal, leaderboard)."""

from __future__ import annotations

import json

import pytest

from dashboard.strategy_dashboard import (
    build_signal_payload,
    compute_momrem_signal,
    load_results_summary,
    render_strategy_page,
)


def test_signal_shape():
    sig = compute_momrem_signal(100_000.0)
    assert sig["regime"]["state"] in ("IN_MARKET", "IN_CASH")
    assert sig["position"]["state"] in ("IN_MARKET", "IN_CASH")
    assert sig["as_of"]  # ISO date present
    assert sig["stale_days"] >= 0
    assert sig["breadth"]["universe_size"] > 0
    assert 0 <= len(sig["basket"]) <= 20
    for b in sig["basket"]:
        assert b["symbol"]
        assert b["weight_pct"] == pytest.approx(100.0 / 20, abs=0.01)
        assert b["last_close"] > 0


def test_signal_capital_changes_basket():
    small = compute_momrem_signal(10_000.0)
    large = compute_momrem_signal(10_000_000.0)
    assert small["capital"] == 10_000.0
    assert large["capital"] == 10_000_000.0
    # a large capital can afford at least as many shares of each name
    for b_small, b_large in zip(small["basket"], large["basket"]):
        assert b_small["symbol"] == b_large["symbol"]
        assert b_large["qty"] >= b_small["qty"]


def test_leaderboard_has_validated_momrem():
    rows = load_results_summary()
    by_family = {r["family"]: r for r in rows}
    assert "momrem" in by_family
    assert by_family["momrem"]["verdict"] == "VALIDATED"
    assert by_family["momrem"]["oos_sharpe"] > 0.9
    # rows sorted by OOS sharpe descending
    sharpe = [r["oos_sharpe"] for r in rows]
    assert sharpe == sorted(sharpe, reverse=True)


def test_payload_json_serialisable():
    payload = build_signal_payload(50_000.0)
    dumped = json.dumps(payload, default=str)
    assert '"strategy": "momrem"' in dumped
    assert payload["signal"]["capital"] == 50_000.0


def test_page_renders_core_sections():
    page = render_strategy_page(100_000.0).decode("utf-8")
    assert "<title>Strategy Dashboard" in page
    assert "VALIDATED" in page
    assert "Research leaderboard" in page
    assert "<svg" in page  # self-contained charts
    assert "fetch_data.py" in page  # refresh instructions present
