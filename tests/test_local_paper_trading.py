"""Tests for the local Upstox-data / virtual-paper boundary."""

from __future__ import annotations

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
