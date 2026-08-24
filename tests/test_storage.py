from datetime import datetime
from pathlib import Path

import pandas as pd

from data.duckdb_manager import DuckDBManager
from data.storage import StorageManager


def test_storage_and_duckdb(tmp_path: Path):
    storage = StorageManager(data_dir=tmp_path)

    data = {
        "date": [datetime(2023, 1, 1), datetime(2023, 1, 2)],
        "Open": [100.0, 105.0],
        "High": [110.0, 115.0],
        "Low": [90.0, 95.0],
        "Close": [105.0, 110.0],
        "Volume": [1000, 2000],
    }
    df = pd.DataFrame(data)

    storage.save_historical_data(df, "yfinance", "NSE", "RELIANCE.NS")

    partition_path = (
        tmp_path / "yfinance" / "NSE" / "RELIANCE.NS" / "2023" / "01.parquet"
    )
    assert partition_path.exists()

    # Test DuckDB
    duckdb_path = tmp_path / "quant.duckdb"
    db_manager = DuckDBManager(db_path=duckdb_path, data_dir=tmp_path)

    # Check if view was created and data is accessible
    result = db_manager.execute("SELECT * FROM market_data")
    assert len(result) == 2
    assert "source" in result.columns

    # Test Snapshot
    snapshot_path = db_manager.create_snapshot("test_snap")
    assert snapshot_path.exists()
