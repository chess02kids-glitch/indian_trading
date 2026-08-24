import sys
from unittest.mock import patch

from main import main


@patch("main.IngestionPipeline")
def test_cli_ingest_symbol(mock_pipeline):
    test_args = ["main.py", "ingest", "--symbol", "RELIANCE.NS", "--period", "1mo"]
    with patch.object(sys, "argv", test_args):
        main()

    mock_pipeline.return_value.ingest_symbol.assert_called_once_with(
        "RELIANCE.NS", period="1mo"
    )


@patch("main.IngestionPipeline")
def test_cli_ingest_universe(mock_pipeline):
    test_args = ["main.py", "ingest", "--universe", "RELIANCE.NS,TCS.NS"]
    with patch.object(sys, "argv", test_args):
        main()

    mock_pipeline.return_value.ingest_universe.assert_called_once_with(
        ["RELIANCE.NS", "TCS.NS"], period="max"
    )


@patch("main.DuckDBManager")
def test_cli_snapshot(mock_db):
    test_args = ["main.py", "snapshot", "--name", "test_snap"]
    with patch.object(sys, "argv", test_args):
        main()

    mock_db.return_value.create_snapshot.assert_called_once_with("test_snap")
