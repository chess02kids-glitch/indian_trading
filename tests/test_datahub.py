"""Tests for the unified data layer and the four defects it fixes.

Every test here corresponds to something that was actually broken:

1. pages disagreeing about whether price data exists,
2. the quote feed sitting in a permanent ERROR state,
3. the strategy signal dying with "no data for signal computation",
4. the Operations page being a table of ``unknown`` placeholders.

Plus the research defect found while reconciling the backtest against the live
signal: the cross-sectional rebalance grid never cleared names that dropped out
of the top-N, so ``ffill()`` carried them forward forever.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from datahub import state as sysstate
from datahub.analytics import (
    divergence_report,
    momrem_targets,
    performance,
    position_sizing,
    regime_summary,
    simulate_weights,
)
from datahub.panel import (
    data_status,
    load_panel,
    materialize_prices,
    select_universe,
    strategy_frame,
)
from datahub.quotes import (
    EodQuoteProvider,
    QuoteChain,
    SimQuoteProvider,
    build_quote_chain,
)


# ---------------------------------------------------------------------------
# Defect 3: universe selection must never silently empty itself
# ---------------------------------------------------------------------------


def test_panel_loads_and_has_ohlcv():
    panel = load_panel()
    assert not panel.empty
    for column in ("open", "high", "low", "close", "volume"):
        assert column in panel.columns
    assert panel.index.names == ["date", "symbol"]


def test_universe_selection_uses_a_recency_window_not_the_last_date():
    """The original bug: a symbol had to print on the single newest panel date.

    The bundle legitimately contains more than one "last bar" date, so a
    long-history universe could end up with zero survivors.
    """
    panel = load_panel()
    close = panel["close"].unstack("symbol").sort_index()
    last_bar = close.apply(lambda col: col.last_valid_index())
    distinct_last_bars = set(pd.Timestamp(x).date() for x in last_bar.dropna())
    # the precondition that triggered the bug is present in the real bundle
    assert len(distinct_last_bars) >= 2, (
        "expected the bundle to contain more than one last-bar date; "
        "if this changed, update the regression test"
    )

    meta = select_universe()
    assert meta["size"] > 0, f"universe emptied itself: {meta['rejected']}"
    # every survivor really did trade inside the recency window
    window_start = pd.Timestamp(meta["recency_window"][0])
    for symbol in meta["symbols"]:
        assert last_bar[symbol] >= window_start, f"{symbol} is stale but was kept"


def test_universe_reports_every_rejection_reason():
    meta = select_universe()
    for key in (
        "insufficient_history",
        "illiquid",
        "insufficient_coverage",
        "not_recently_traded",
    ):
        assert key in meta["rejected"]
    kept = set(meta["symbols"])
    for names in meta["rejected"].values():
        assert kept.isdisjoint(names), "a symbol was both kept and rejected"


def test_strategy_frame_is_trimmed_to_the_universes_own_last_bar():
    """The frame must not end on an all-NaN row, or momentum scoring dies."""
    close, meta = strategy_frame()
    assert not close.empty
    assert close.iloc[-1].notna().sum() > 0, "final row is entirely NaN"
    assert meta["as_of"] == str(close.index[-1].date())


# ---------------------------------------------------------------------------
# Defect 1: one data status, shared by every page
# ---------------------------------------------------------------------------


def test_materialized_prices_file_matches_the_shared_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_STATE_FILE", str(tmp_path / "state.json"))
    result = materialize_prices(force=True)
    assert result["exists"] is True
    assert result["rows"] and result["rows"] > 0

    status = data_status(refresh=True)
    assert status["available"] is True
    # the cockpit's fields and the strategy dashboard's panel must agree
    assert status["prices_exists"] is True
    assert status["prices_info"]["symbols"] == load_panel().index.get_level_values(
        "symbol"
    ).nunique()
    assert status["universe"]["size"] > 0

    frame = pd.read_parquet(result["path"])
    assert {"date", "symbol", "open", "high", "low", "close", "volume"} <= set(
        frame.columns
    )


def test_research_api_data_status_agrees_with_datahub():
    from dashboard.research_api import get_data_status

    status = get_data_status()
    hub = data_status()
    assert status["prices_exists"] is True, "cockpit still reports missing data"
    assert status["prices_info"]["symbols"] == hub["prices_info"]["symbols"]
    assert status["prices_info"]["dates"] == hub["prices_info"]["dates"]


def test_explicit_prices_path_still_inspected_directly(tmp_path):
    from dashboard.research_api import get_data_status

    dummy = tmp_path / "custom.csv"
    dummy.write_text("date,symbol,close\n2020-01-01,AAA,100\n2020-01-02,AAA,101\n")
    status = get_data_status(prices_path=dummy)
    assert status["prices_exists"] is True
    assert status["prices_info"]["format"] == "long"
    missing = get_data_status(prices_path=tmp_path / "nope.parquet")
    assert missing["prices_exists"] is False


# ---------------------------------------------------------------------------
# Defect 2: the quote chain degrades instead of failing
# ---------------------------------------------------------------------------


class _NoTokenMarketData:
    """Stands in for UpstoxMarketData with no access token."""

    access_token = ""

    def connection_status(self):
        return {"configured": False, "mode": "UPSTOX_DATA", "detail": "no token"}

    def fetch_quotes(self, instruments):  # pragma: no cover - never reached
        raise AssertionError("must not be called without a token")


def test_quote_chain_falls_back_to_sim_without_a_token():
    chain = build_quote_chain(_NoTokenMarketData(), {"RELIANCE": "NSE_EQ|INE002A01018"})
    quotes = chain.fetch(["RELIANCE", "HDFCBANK"])
    assert quotes, "the chain produced no quotes at all"
    for symbol, quote in quotes.items():
        assert quote.source in ("SIM", "EOD")
        assert quote.last_price > 0
    summary = chain.summarise(quotes)
    assert summary["source"] in ("SIM", "EOD")


def test_quote_chain_never_labels_simulated_data_as_live():
    chain = build_quote_chain(_NoTokenMarketData(), {})
    quotes = chain.fetch(["TCS"])
    assert all(q.source != "UPSTOX" for q in quotes.values())
    assert chain.primary_source != "UPSTOX"


def test_quote_chain_can_disable_the_simulator():
    chain = build_quote_chain(_NoTokenMarketData(), {}, allow_sim=False)
    names = [p.name for p in chain.providers]
    assert names == ["UPSTOX", "EOD"]


def test_index_symbols_are_anchored():
    """NIFTY_50 is not an equity, so it is absent from the price panel."""
    quotes = SimQuoteProvider().fetch(["NIFTY_50"])
    assert "NIFTY_50" in quotes
    assert quotes["NIFTY_50"].last_price > 0
    assert quotes["NIFTY_50"].source == "SIM"


def test_paper_service_reports_sim_not_error(tmp_path):
    from paper_trading import PaperLedger, PaperTradingService

    service = PaperTradingService(
        root=".", ledger=PaperLedger(tmp_path / "paper.sqlite"),
        market_data=_NoTokenMarketData(),
    )
    result = service.refresh_quotes()
    assert result["quote_status"] == "SIM"
    assert result["quote_error"] is None
    health = service.status()["quote_health"]
    assert health["status"] == "HEALTHY_SIM"
    assert health["error"] is None


# ---------------------------------------------------------------------------
# Defect 4: Operations has no placeholder values
# ---------------------------------------------------------------------------


def test_operations_report_has_no_unknown_placeholders(tmp_path):
    from dashboard.operations import build_report

    monkey_state = tmp_path / "state.json"
    old = sysstate.state_path()
    import os

    os.environ["QUANT_STATE_FILE"] = str(monkey_state)
    try:
        report = build_report(paper=None, signal=None, market_data=None, feed=None)
    finally:
        os.environ["QUANT_STATE_FILE"] = str(old)

    assert report["headline"][0] in (
        "HEALTHY", "ACTION", "DEGRADED", "DIVERGED", "COLD START", "STALE", "HALTED"
    )
    assert report["broker_health"]["state"] != "unknown"
    assert report["reconciliation"]["state"] != "unknown"
    assert report["system_health"]["overall"] != "unknown"
    # every declared heartbeat is present with an explicit state
    beats = report["system_health"]["heartbeats"]
    assert set(beats) == set(sysstate.HEARTBEATS)
    for beat in beats.values():
        assert beat["state"] in ("ok", "stale", "never")


def test_kill_switch_blocks_paper_rebalancing(tmp_path):
    from paper_trading import PaperLedger, PaperTradingService

    os_env = __import__("os").environ
    os_env["QUANT_STATE_FILE"] = str(tmp_path / "state.json")
    try:
        sysstate.set_kill_switch(True, reason="test", armed_by="pytest")
        assert sysstate.is_killed() is True
        service = PaperTradingService(
            root=".", ledger=PaperLedger(tmp_path / "paper.sqlite"),
            market_data=_NoTokenMarketData(),
        )
        with pytest.raises(ValueError, match="kill switch"):
            service.execute_rebalance("momrem", "PAPER REBALANCE")
        automation = service.run_automation_once()
        assert automation["ran"] is False
        assert automation["reason"] == "kill switch is armed"
        assert service.status()["kill_switch"]["armed"] is True
    finally:
        sysstate.set_kill_switch(False, reason="", armed_by="pytest")
        os_env.pop("QUANT_STATE_FILE", None)


# ---------------------------------------------------------------------------
# The research defect: the top-N basket must actually hold N names
# ---------------------------------------------------------------------------


def _toy_panel(n_symbols=40, n_days=400, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2022-01-03", periods=n_days)
    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    rets = rng.normal(0.0004, 0.02, size=(n_days, n_symbols))
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=symbols
    )
    return close


def test_momrem_targets_hold_exactly_top_n_names():
    """Regression: the old grid never cleared dropouts, so ffill() accumulated
    the union of every name ever selected (~272 held instead of 20)."""
    close = _toy_panel()
    targets = momrem_targets(close, top_n=5, rebalance=20, apply_regime=False)
    held = (targets > 0).sum(axis=1)
    invested = held[held > 0]
    assert len(invested) > 0
    assert int(invested.max()) <= 5, f"held {int(invested.max())} names, expected <= 5"
    # and the gross exposure of an invested row is 1.0, not N * (1/N)
    assert float(targets.sum(axis=1).max()) <= 1.0 + 1e-9


def test_research_live_mom_overlay_no_longer_accumulates():
    """The same invariant, checked against the patched research module."""
    pytest.importorskip("research_live.mom_overlay")
    from research_live.mom_overlay import mom_tgt

    close = _toy_panel()
    targets = mom_tgt(close, lookback=20, hold=20, top_n=5, risk_kind=None)
    held = (targets > 0).sum(axis=1)
    invested = held[held > 0]
    assert int(invested.max()) <= 5


def test_datahub_and_research_implementations_agree():
    """Both code paths must describe the same strategy."""
    close = _toy_panel()
    from research_live.mom_overlay import mom_tgt

    mine = momrem_targets(close, apply_regime=False)
    theirs = mom_tgt(close, lookback=20, hold=20, top_n=20, risk_kind=None)
    # same names selected on each rebalance date
    for i in range(0, len(close), 20):
        a = set(mine.iloc[i][mine.iloc[i] > 0].index)
        b = set(theirs.iloc[i][theirs.iloc[i] > 0].index)
        assert a == b, f"selection diverged on {close.index[i].date()}"


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


def test_performance_metrics_are_sane():
    close = _toy_panel()
    targets = momrem_targets(close)
    sim = simulate_weights(close, targets)
    metrics = performance(sim["returns"], sim["equity"])
    assert metrics["n_days"] > 100
    assert -1.0 < metrics["max_dd"] <= 0.0
    assert metrics["vol"] > 0
    assert 0.0 <= metrics["win_rate"] <= 1.0


def test_cost_sensitivity_degrades_monotonically():
    from datahub.analytics import cost_sensitivity

    close = _toy_panel(n_days=600)
    grid = [2.5, 10.0, 20.0, 40.0]
    result = cost_sensitivity(close, costs_bps=grid, oos_start=None)
    sharpes = [row["sharpe"] for row in result["grid"]]
    assert sharpes == sorted(sharpes, reverse=True), (
        f"higher costs must not improve Sharpe: {sharpes}"
    )
    cagrs = [row["cagr"] for row in result["grid"]]
    assert cagrs == sorted(cagrs, reverse=True)


def test_regime_summary_labels_the_current_state():
    close = _toy_panel(n_days=500)
    summary = regime_summary(close)
    current = summary["current"]
    assert current["filter"] in ("IN_MARKET", "IN_CASH")
    assert current["trend"] in ("TREND_UP", "TREND_DOWN", "CHOPPY")
    assert current["vol_regime"] in ("LOW_VOL", "NORMAL_VOL", "HIGH_VOL")
    assert summary["segments"], "no regime segments were produced"
    for segment in summary["segments"]:
        assert segment["color"].startswith("#")


def test_regime_segments_are_labelled_from_their_own_chunk():
    """Regression: segments used to index the *whole* label series at position
    0, so every segment rendered with the first row's label and one colour while
    ``current.label`` said something else entirely."""
    close = _toy_panel(n_days=500)
    summary = regime_summary(close)
    segments = summary["segments"]
    colors = summary["colors"]

    # the newest segment must agree with the reported current state
    assert segments[-1]["label"] == summary["current"]["label"]
    assert segments[-1]["end"] == summary["current"]["as_of"]
    # and the history must actually contain more than one regime
    assert len({s["label"] for s in segments}) > 1, (
        "every segment carries the same label — the chunk-indexing bug is back"
    )
    # every colour must resolve from that segment's own label
    for segment in segments:
        root = segment["label"].split(" · ")[0]
        assert root in colors, f"unknown regime root {root!r}"
        assert segment["color"] == colors[root]
        assert segment["days"] > 0
    # segments must be contiguous and non-overlapping, in date order
    starts = [s["start"] for s in segments]
    assert starts == sorted(starts)


def test_regime_tagged_equity_colours_by_actual_regime():
    close = _toy_panel(n_days=500)
    dates = [str(d.date()) for d in close.index[-120:]]
    tagged = _regime_tagged(dates, close)
    assert len(tagged) == len(dates)
    assert len({t["label"] for t in tagged}) > 1
    assert all(t["color"].startswith("#") for t in tagged)


def _regime_tagged(dates, close):
    from datahub.analytics import regime_tagged_equity

    return regime_tagged_equity(dates, close)


def test_divergence_needs_two_sessions():
    report = divergence_report([], initial_capital=100000)
    assert report["ready"] is False
    assert "at least two" in report["reason"] or "same day" in report["reason"]


def test_divergence_flags_a_large_drift():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = []
    for day in range(30):
        stamp = (start + timedelta(days=day)).isoformat()
        # a 40% collapse in a month is many sigma away from a 19% CAGR path
        equity = 100000 * (1 - 0.4 * day / 29)
        points.append({"timestamp": stamp, "equity": equity})
    report = divergence_report(points, initial_capital=100000)
    assert report["ready"] is True
    assert report["state"] == "INVESTIGATE"
    assert report["summary"]["z_score"] < -2
    assert report["summary"]["tracking_error"] > 0
    assert len(report["series"]) >= 20


def test_divergence_accepts_a_calm_account():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = [
        {"timestamp": (start + timedelta(days=d)).isoformat(), "equity": 100000 + d * 10}
        for d in range(40)
    ]
    report = divergence_report(points, initial_capital=100000)
    assert report["ready"] is True
    assert report["state"] in ("ON TRACK", "WATCH")


def test_position_sizing_caps_the_largest_position():
    close = _toy_panel(n_symbols=20, n_days=300)
    basket = [
        {"symbol": symbol, "last_close": 100.0} for symbol in list(close.columns)[:10]
    ]
    result = position_sizing(
        capital=1_000_000,
        basket=basket,
        close=close,
        max_position_weight=0.15,
        monte_carlo_paths=200,
    )
    assert len(result["rows"]) == 10
    assert max(r["vol_target_weight_pct"] for r in result["rows"]) <= 15.0 + 1e-6
    assert 0 <= result["kelly"]["recommended_pct"] <= result["kelly"]["full_kelly_pct"]
    assert result["risk_of_ruin_pct"] is not None
    assert 0 <= result["risk_of_ruin_pct"] <= 100


# ---------------------------------------------------------------------------
# Universe expansion
# ---------------------------------------------------------------------------


def test_universe_expansion_reports_state_without_building():
    from datahub.universe import status

    state = status()
    assert state["bundle_symbols"] > 0
    assert state["raw_files"] > state["bundle_symbols"]
    assert "build" in state


def test_expand_universe_script_dry_run(capsys):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.expand_universe import main

    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "raw symbols" in out


# ---------------------------------------------------------------------------
# JSON serialisation: analytics emit NaN for warm-up windows, and json.dumps()
# would turn that into invalid JSON on the wire.
# ---------------------------------------------------------------------------


def test_json_safe_replaces_non_finite_values():
    import numpy as np

    from dashboard.server import _json_safe

    payload = {
        "nan": float("nan"),
        "inf": float("inf"),
        "neg_inf": float("-inf"),
        "np_nan": np.float64("nan"),
        "np_float": np.float64(1.5),
        "np_int": np.int64(7),
        "keep_bool": True,
        "keep_none": None,
        "keep_str": "text",
        "nested": [float("nan"), {"deep": float("inf")}],
        "tuple": (1, 2),
    }
    safe = _json_safe(payload)
    for key in ("nan", "inf", "neg_inf", "np_nan"):
        assert safe[key] is None, f"{key} should have become null"
    assert safe["nested"][0] is None and safe["nested"][1]["deep"] is None
    # bools must stay bools, not ints, and numpy scalars must stay numbers
    assert safe["keep_bool"] is True
    assert safe["np_int"] == 7 and isinstance(safe["np_int"], int)
    assert safe["np_float"] == 1.5 and isinstance(safe["np_float"], float)
    assert safe["tuple"] == [1, 2]


def test_json_safe_output_is_always_strictly_parseable():
    import json

    import numpy as np

    from dashboard.server import _json_safe

    def _boom(constant):
        raise ValueError(f"non-finite constant {constant!r}")

    payload = {
        "a": float("nan"),
        "b": [np.float32("inf"), {"c": float("-inf")}],
        "d": np.float64(2.5),
        "e": {1, 2},
    }
    raw = json.dumps(_json_safe(payload), allow_nan=False, default=str)
    assert json.loads(raw, parse_constant=_boom) is not None


def test_json_safe_survives_a_self_referencing_payload():
    from dashboard.server import _json_safe

    payload: dict[str, Any] = {"name": "loop"}
    payload["self"] = payload
    # must terminate rather than hang the server thread
    assert _json_safe(payload)["name"] == "loop"


# ---------------------------------------------------------------------------
# The Operations regime colouring fix, checked through the HTTP layer
# ---------------------------------------------------------------------------


def test_regime_endpoint_exposes_a_real_proxy_series():
    from dashboard.api import regime_payload

    payload = regime_payload()
    series = payload["proxy_series"]
    assert len(series) > 50, "expected a downsampled but substantial series"
    current = payload["summary"]["current"]
    # the series must end on the newest bar and agree with the current regime
    assert series[-1]["date"] == current["as_of"]
    assert series[-1]["label"] == current["label"]
    # warm-up rows have no 100d SMA yet; they must be null, not NaN
    assert any(point["sma"] is None for point in series[:20])
    assert all(point["proxy"] is not None for point in series)
    # and it must actually be a proxy-vs-SMA chart, not one line drawn twice
    differ = [p for p in series if p["sma"] is not None and p["proxy"] != p["sma"]]
    assert len(differ) > len(series) // 2, "proxy and SMA are identical — placeholder bug"
