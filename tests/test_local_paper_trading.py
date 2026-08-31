"""Tests for the local Upstox-data / virtual-paper boundary."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dashboard.paper_trading import render_paper_trading_page
from paper_trading.ledger import PaperLedger
from paper_trading.market_data import (
    MarketDataUnavailable,
    MarketQuote,
    UpstoxMarketData,
)
from paper_trading.service import PaperTradingService


class FakeMarketData:
    """Quote-only fake: deliberately contains no order-related method."""

    def connection_status(self):
        return {"configured": True, "mode": "FAKE", "detail": "fake quote feed"}

    def fetch_quotes(self, instruments):
        return {
            symbol: MarketQuote(
                symbol=symbol,
                instrument_key=instrument_key,
                last_price=100.0,
                bid_price=99.5,
                ask_price=100.5,
                volume=10_000,
                timestamp=datetime.now(UTC),
                source="fake",
            )
            for symbol, instrument_key in instruments.items()
        }


class StaleMarketData(FakeMarketData):
    """A read-only source that exposes an old source timestamp."""

    def fetch_quotes(self, instruments):
        return {
            symbol: MarketQuote(
                symbol=symbol,
                instrument_key=instrument_key,
                last_price=100.0,
                bid_price=99.5,
                ask_price=100.5,
                volume=10_000,
                timestamp=datetime(2000, 1, 1, tzinfo=UTC),
                source="fake",
            )
            for symbol, instrument_key in instruments.items()
        }


def _service(tmp_path: Path) -> PaperTradingService:
    universe = tmp_path / "data" / "universe" / "nifty500-pit"
    universe.mkdir(parents=True)
    universe.joinpath("nifty500.csv").write_text(
        "symbol,index_name,valid_from,valid_to,isin,sector,exchange,delisted\n"
        "RELIANCE,nifty500,2020-01-01,,INE002A01018,,,\n"
        "HDFCBANK,nifty500,2020-01-01,,INE040A01034,,,\n"
        "ICICIBANK,nifty500,2020-01-01,,INE090A01021,,,\n"
        "TCS,nifty500,2020-01-01,,INE467B01029,,,\n",
        encoding="utf-8",
    )
    return PaperTradingService(
        root=tmp_path,
        ledger=PaperLedger(tmp_path / "var" / "paper.sqlite"),
        market_data=FakeMarketData(),
    )


def test_paper_page_describes_its_virtual_read_only_boundary():
    page = render_paper_trading_page()
    assert b"PAPER ONLY" in page
    assert b"/api/paper/rebalance" in page
    assert b"Nothing on this page can place a real order" in page


def test_market_data_requires_an_access_token_not_an_app_key(monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    feed = UpstoxMarketData.from_environment()
    assert feed.connection_status()["configured"] is False
    with pytest.raises(MarketDataUnavailable, match="UPSTOX_ACCESS_TOKEN"):
        feed.fetch_quotes({"RELIANCE": "NSE_EQ|INE002A01018"})


def test_virtual_fill_uses_persisted_local_ledger_and_live_quote_marks(tmp_path):
    service = _service(tmp_path)
    service.start_monitor()
    mark = service.refresh_quotes()
    assert mark["quote_status"] == "LIVE"

    fill = service.ledger.execute_virtual_fill(
        strategy_id="test_strategy",
        symbol="RELIANCE",
        side="BUY",
        quantity=10,
        fill_price=100.5,
        charges=2.0,
        source="fake_quote_read_only",
        quote_timestamp=datetime.now(UTC).isoformat(),
    )
    assert fill["status"] == "FILLED"
    marked = service.refresh_quotes()
    assert marked["equity"] == pytest.approx(999_993.0)

    status = service.status()
    assert status["paper_only"] is True
    assert status["positions"][0]["symbol"] == "RELIANCE"
    assert status["positions"][0]["last_price"] == 100.0
    assert status["orders"][0]["source"] == "fake_quote_read_only"


def test_strategy_cannot_rebalance_until_research_gate_approves_it(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="not paper-approved"):
        service.preview_rebalance("momrem")


def test_reset_requires_deliberate_paper_confirmation(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="RESET PAPER"):
        service.reset("reset")
    service.start_monitor()
    assert (
        service.reset("RESET PAPER", 500_000)["settings"]["initial_capital"] == 500_000
    )


def test_watchlist_risk_health_and_automation_are_paper_gated(tmp_path):
    service = _service(tmp_path)
    status = service.set_watchlist(["NIFTY_50", "TCS", "TCS"])
    assert status["settings"]["watchlist"] == ["NIFTY_50", "TCS"]
    assert service.refresh_quotes()["quote_status"] == "LIVE"
    status = service.status()
    assert status["quote_health"]["status"] == "HEALTHY"
    assert [item["symbol"] for item in status["watchlist_quotes"]] == [
        "NIFTY_50",
        "TCS",
    ]

    with pytest.raises(ValueError, match="max_position_weight"):
        service.set_risk_policy({"max_position_weight": 2})
    with pytest.raises(ValueError, match="only a paper-approved"):
        service.set_auto_paper(
            enabled=True,
            strategy_id="momrem",
            confirmation="ENABLE AUTO PAPER",
        )
    disabled = service.set_auto_paper(enabled=False, strategy_id="", confirmation="")
    assert disabled["settings"]["auto_paper_enabled"] is False


def test_explicitly_approved_strategy_can_enable_virtual_automation(tmp_path):
    service = _service(tmp_path)
    service.registry_path.parent.mkdir(parents=True)
    service.registry_path.write_text(
        '{"strategies":{"momrem":{"paper_approved":true,"min_rebalance_seconds":60}}}',
        encoding="utf-8",
    )
    enabled = service.set_auto_paper(
        enabled=True,
        strategy_id="momrem",
        confirmation="ENABLE AUTO PAPER",
    )
    assert enabled["settings"]["auto_paper_enabled"] is True
    assert enabled["settings"]["auto_strategy"] == "momrem"
    assert enabled["settings"]["auto_interval_seconds"] == 60
    assert service.ledger.all_orders() == []


def test_closed_positions_keep_lifetime_realised_pnl_and_audit_export(tmp_path):
    service = _service(tmp_path)
    service.ledger.execute_virtual_fill(
        strategy_id="test_strategy",
        symbol="RELIANCE",
        side="BUY",
        quantity=10,
        fill_price=100.0,
        charges=1.0,
        source="fake_quote_read_only",
        quote_timestamp=datetime.now(UTC).isoformat(),
    )
    service.ledger.execute_virtual_fill(
        strategy_id="test_strategy",
        symbol="RELIANCE",
        side="SELL",
        quantity=10,
        fill_price=110.0,
        charges=1.0,
        source="fake_quote_read_only",
        quote_timestamp=datetime.now(UTC).isoformat(),
    )
    closed = service.ledger.all_positions()[0]
    assert closed["quantity"] == 0
    assert closed["realized_pnl"] == pytest.approx(98.0)
    assert service.status()["portfolio"]["realized_pnl"] == pytest.approx(98.0)
    assert service.audit()["passed"] is True
    orders_csv = service.export_csv("orders")
    assert "test_strategy" in orders_csv
    assert "RELIANCE" in service.export_csv("positions")
    with pytest.raises(ValueError, match="export dataset"):
        service.export_csv("secrets")


def test_pretrade_risk_blocks_an_oversized_virtual_target(tmp_path):
    service = _service(tmp_path)
    service.set_risk_policy({"max_position_weight": 0.10})
    quote = FakeMarketData().fetch_quotes({"RELIANCE": "NSE_EQ|INE002A01018"})[
        "RELIANCE"
    ]
    risk = service._pretrade_risk({"RELIANCE": 20_000}, {"RELIANCE": quote}, 1)
    assert risk["allowed"] is False
    assert "max_position_weight: RELIANCE" in risk["breaches"]
    assert "max_gross_exposure" in risk["breaches"]


def test_quote_health_marks_old_source_quotes_stale(tmp_path):
    service = _service(tmp_path)
    service.market_data = StaleMarketData()
    service.refresh_quotes()
    health = service.status()["quote_health"]
    assert health["status"] == "STALE"
    assert set(health["stale_symbols"]) == {
        "NIFTY_50",
        "RELIANCE",
        "HDFCBANK",
        "ICICIBANK",
        "TCS",
    }


def test_preview_applies_risk_guard_before_any_virtual_fill(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.registry_path.parent.mkdir(parents=True)
    service.registry_path.write_text(
        '{"strategies":{"momrem":{"paper_approved":true}}}', encoding="utf-8"
    )
    service.set_risk_policy({"max_position_weight": 0.10})
    monkeypatch.setattr(
        service,
        "_momrem_target",
        lambda: ({"RELIANCE": 20_000}, {"fresh": True, "position": {"state": "LONG"}}),
    )
    preview = service.preview_rebalance("momrem")
    assert preview["ready"] is False
    assert "max_position_weight: RELIANCE" in preview["reason"]
    assert service.ledger.all_orders() == []


def test_existing_ledger_is_upgraded_without_erasing_settings(tmp_path):
    path = tmp_path / "var" / "old.sqlite"
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE paper_settings (
                id INTEGER PRIMARY KEY CHECK(id = 1), initial_capital REAL NOT NULL,
                cash REAL NOT NULL, running INTEGER NOT NULL DEFAULT 0,
                data_mode TEXT NOT NULL DEFAULT 'UPSTOX_DATA', active_strategy TEXT,
                started_at TEXT, paused_at TEXT, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO paper_settings
            (id, initial_capital, cash, running, data_mode, updated_at)
            VALUES (1, 123456, 123456, 0, 'UPSTOX_DATA', '2026-01-01T00:00:00+00:00')
            """
        )
    settings = PaperLedger(path).settings()
    assert settings["initial_capital"] == 123456
    assert settings["watchlist"][0] == "NIFTY_50"
    assert settings["auto_paper_enabled"] is False
