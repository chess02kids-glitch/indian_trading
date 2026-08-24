from datetime import datetime

import pandas as pd
import pytest

from ingestion.validation import ValidationEngine


def test_validation_engine_success():
    engine = ValidationEngine("RELIANCE.NS")

    data = {
        "date": [datetime(2023, 1, 1), datetime(2023, 1, 2)],
        "Open": [100.0, 105.0],
        "High": [110.0, 115.0],
        "Low": [90.0, 95.0],
        "Close": [105.0, 110.0],
        "Volume": [1000, 2000],
    }
    df = pd.DataFrame(data)

    # Should pass without raising
    valid_df = engine.validate_df(df)
    assert not valid_df.empty


def test_validation_engine_high_low_failure():
    engine = ValidationEngine("RELIANCE.NS")

    data = {
        "date": [datetime(2023, 1, 1)],
        "Open": [100.0],
        "High": [90.0],  # High < Low
        "Low": [95.0],
        "Close": [92.0],
        "Volume": [1000],
    }
    df = pd.DataFrame(data)

    with pytest.raises(ValueError, match="Data validation failed"):
        engine.validate_df(df)


def test_validation_engine_duplicates_failure():
    engine = ValidationEngine("RELIANCE.NS")

    data = {
        "date": [datetime(2023, 1, 1), datetime(2023, 1, 1)],  # Duplicate
        "Open": [100.0, 100.0],
        "High": [110.0, 110.0],
        "Low": [90.0, 90.0],
        "Close": [105.0, 105.0],
        "Volume": [1000, 1000],
    }
    df = pd.DataFrame(data)

    with pytest.raises(ValueError, match="Duplicate records found"):
        engine.validate_df(df)
