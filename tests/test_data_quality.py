"""Phase 2 / final-suite tests for OHLCV validation and data quality."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from data.quality import (
    DataQualityError,
    check_ohlcv_long_frame,
    detect_data_staleness,
    detect_missing_candles,
    load_market_bars,
    validate_market_bars,
)
from models.domain import MarketBar

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _good_rows(**overrides) -> list[dict]:
    base = [
        {
            "date": "2026-08-20",
            "symbol": "RELIANCE",
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1_000,
        },
        {
            "date": "2026-08-21",
            "symbol": "RELIANCE",
            "open": 104.0,
            "high": 108.0,
            "low": 103.0,
            "close": 107.0,
            "volume": 1_200,
        },
    ]
    if overrides:
        base[0].update(overrides)
    return base


class TestOhlcvValidation:
    def test_valid_frame_passes(self) -> None:
        accepted, report = check_ohlcv_long_frame(_frame(_good_rows()))
        assert len(accepted) == 2
        assert report.is_clean

    def test_final_suite_malformed_ohlc_rejected(self) -> None:
        """Final suite (test 12): malformed market data is rejected, not accepted."""
        rows = _good_rows(high=98.0)  # high < max(open, close)
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        kinds = {issue.kind for issue in report.issues}
        assert "ohlc_inconsistency" in kinds
        assert len(accepted) == 1
        assert report.accepted_rows == 1

    def test_low_above_close_rejected(self) -> None:
        rows = _good_rows(low=101.0)  # low > min(open, close)
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        assert {issue.kind for issue in report.issues} >= {"ohlc_inconsistency"}
        assert len(accepted) == 1

    def test_zero_close_rejected(self) -> None:
        rows = _good_rows(close=0.0)
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        assert any(issue.kind == "invalid_close" for issue in report.issues)
        assert len(accepted) == 1

    def test_negative_volume_rejected(self) -> None:
        rows = _good_rows(volume=-1.0)
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        assert any(issue.kind == "invalid_volume" for issue in report.issues)
        assert len(accepted) == 1

    def test_non_numeric_price_rejected(self) -> None:
        rows = _good_rows(close="not-a-number")
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        assert any(issue.kind == "invalid_close" for issue in report.issues)
        assert len(accepted) == 1

    def test_unparseable_date_rejected(self) -> None:
        rows = _good_rows(date="not-a-date")
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        assert any(issue.kind == "invalid_timestamp" for issue in report.issues)
        assert len(accepted) == 1

    def test_duplicate_row_rejected(self) -> None:
        rows = _good_rows()
        rows.append(dict(rows[0]))
        accepted, report = check_ohlcv_long_frame(_frame(rows))
        assert any(issue.kind == "duplicate_row" for issue in report.issues)
        assert len(accepted) == 1

    def test_empty_frame_raises(self) -> None:
        with pytest.raises(DataQualityError):
            check_ohlcv_long_frame(pd.DataFrame())

    def test_missing_columns_raise(self) -> None:
        with pytest.raises(DataQualityError, match="missing columns"):
            check_ohlcv_long_frame(_frame([{"date": "2026-01-01", "symbol": "A"}]))

    def test_invalid_records_are_never_silently_filled(self) -> None:
        """Bad rows are excluded, and gaps are reported — never imputed."""
        rows = _good_rows()  # RELIANCE 20th + 21st
        rows.append(
            {
                "date": "2026-08-20",
                "symbol": "TCS",
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 1_000,
            }
        )
        frame = _frame(rows)
        accepted, _ = check_ohlcv_long_frame(frame)
        issues = validate_market_bars(frame)[1]
        kinds = {issue.kind for issue in issues.issues}
        # TCS missing 2026-08-21 is reported, not filled.
        assert "missing_candle" in kinds
        # No new rows were created: 3 in, 3 out.
        assert len(accepted) == 3
        assert issues.accepted_rows == 3


class TestStaleness:
    def test_fresh_data(self) -> None:
        frame = _frame(_good_rows())
        assert detect_data_staleness(frame, reference_now=NOW) is None

    def test_stale_data_reported(self) -> None:
        frame = _frame(_good_rows())
        stale_now = NOW + timedelta(days=10)
        issue = detect_data_staleness(frame, reference_now=stale_now)
        assert issue is not None
        assert issue.kind == "staleness"

    def test_custom_limit(self) -> None:
        frame = _frame(_good_rows())
        issue = detect_data_staleness(
            frame, reference_now=NOW + timedelta(days=3), max_staleness_days=2.0
        )
        assert issue is not None


class TestMissingCandles:
    def test_gap_detected(self) -> None:
        accepted, _ = check_ohlcv_long_frame(_frame(_good_rows()))
        # Simulate TCS missing the middle date.
        frame = _frame(
            _good_rows()
            + [
                {
                    "date": "2026-08-20",
                    "symbol": "TCS",
                    "open": 50.0,
                    "high": 52.0,
                    "low": 49.0,
                    "close": 51.0,
                    "volume": 10,
                }
            ]
        )
        accepted, _ = check_ohlcv_long_frame(frame)
        issues = detect_missing_candles(accepted)
        kinds = {(i.kind, i.symbol) for i in issues}
        assert ("missing_candle", "TCS") in kinds
        # RELIANCE has no missing candles.
        assert not any(
            i.symbol == "RELIANCE" and i.kind == "missing_candle" for i in issues
        )


class TestStrictMarketBars:
    def test_load_bars_round_trip(self) -> None:
        frame = _frame(_good_rows())
        bars, report = load_market_bars(frame)
        assert len(bars) == 2
        assert all(isinstance(bar, MarketBar) for bar in bars)
        assert bars[0].symbol == "RELIANCE"
        assert report.is_clean

    def test_load_bars_reports_strict_rejections(self) -> None:
        rows = _good_rows()
        rows.append(
            {
                "date": "2026-08-22",
                "symbol": "RELIANCE",
                "open": 107.0,
                "high": 107.5,
                "low": 106.0,
                "close": 108.0,  # close > high -> strict rejection
                "volume": 500,
            }
        )
        bars, report = load_market_bars(_frame(rows))
        assert len(bars) == 2
        assert any(
            issue.kind in ("ohlc_inconsistency", "strict_validation_failed")
            for issue in report.issues
        )
