from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ingestion.pipeline import IngestionPipeline
from ingestion.yfinance_client import YFinanceClient


@patch("ingestion.yfinance_client.yf.Ticker")
def test_yfinance_client_fetch_success(mock_ticker):
    mock_instance = MagicMock()
    mock_ticker.return_value = mock_instance

    data = {
        "Date": [datetime(2023, 1, 1)],
        "Open": [100.0],
        "High": [110.0],
        "Low": [90.0],
        "Close": [105.0],
        "Volume": [1000],
    }
    df = pd.DataFrame(data).set_index("Date")
    mock_instance.history.return_value = df

    client = YFinanceClient()
    result = client.fetch_symbol("RELIANCE.NS")

    assert not result.empty
    assert "date" in result.columns
    assert result.iloc[0]["Open"] == 100.0


@patch("ingestion.yfinance_client.yf.Ticker")
def test_yfinance_client_fetch_retry_failure(mock_ticker):
    mock_instance = MagicMock()
    mock_ticker.return_value = mock_instance

    # Simulate exception
    mock_instance.history.side_effect = Exception("API Error")

    client = YFinanceClient()
    with pytest.raises(Exception, match="API Error"):
        # Should raise after retries
        with patch("time.sleep", return_value=None):
            client.fetch_symbol("RELIANCE.NS")


@patch("ingestion.pipeline.YFinanceClient.fetch_symbol")
@patch("ingestion.pipeline.StorageManager.save_historical_data")
def test_pipeline_ingest_symbol(mock_save, mock_fetch):
    data = {
        "date": [datetime(2023, 1, 1)],
        "Open": [100.0],
        "High": [110.0],
        "Low": [90.0],
        "Close": [105.0],
        "Volume": [1000],
    }
    df = pd.DataFrame(data)

    mock_fetch.return_value = df

    pipeline = IngestionPipeline()
    pipeline.ingest_symbol("RELIANCE.NS")

    mock_fetch.assert_called_once_with("RELIANCE.NS", "max")
    mock_save.assert_called_once()
