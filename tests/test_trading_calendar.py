"""Tests for trading-calendar validation (off-calendar candle detection)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from data.quality import (
    TradingCalendar,
    detect_off_calendar_candles,
    nse_weekday_calendar,
    validate_market_bars,
)


def _frame(days: list[str], symbol: str = "A") -> pd.DataFrame:
    rows = []
    for day in days:
        rows.append(
            {
                "date": day,
                "symbol": symbol,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows)


def test_nse_weekday_calendar() -> None:
    calendar = nse_weekday_calendar()
    # 2024-01-08 is a Monday (trading), 2024-01-06 is a Saturday (closed).
    assert calendar.is_trading_day(date(2024, 1, 8))
    assert not calendar.is_trading_day(date(2024, 1, 6))
    assert not calendar.is_trading_day(date(2024, 1, 7))


def test_holidays_are_not_trading_days() -> None:
    calendar = TradingCalendar(
        holidays=[date(2024, 1, 26)]  # Republic Day
    )
    assert not calendar.is_trading_day(date(2024, 1, 26))
    assert calendar.is_trading_day(date(2024, 1, 25))


def test_detect_off_calendar_candles() -> None:
    frame = _frame(["2024-01-08", "2024-01-06"])  # Monday + Saturday
    issues = detect_off_calendar_candles(frame)
    assert any(i.kind == "off_calendar" for i in issues)
    calendar_issues = [i for i in issues if i.kind == "off_calendar"]
    assert calendar_issues[0].date == "2024-01-06"


def test_validate_market_bars_reports_off_calendar() -> None:
    frame = _frame(["2024-01-08", "2024-01-09", "2024-01-07"])
    accepted, report = validate_market_bars(frame)
    kinds = {i.kind for i in report.issues}
    assert "off_calendar" in kinds
    # The off-calendar row is still accepted (reported, not dropped/filled).
    assert len(accepted) == 3
