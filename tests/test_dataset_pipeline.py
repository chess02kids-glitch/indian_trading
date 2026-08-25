"""Tests for the clean dataset pipeline (clean Parquet + metadata + DuckDB)."""

from __future__ import annotations

import pandas as pd
import pytest

from data.dataset import CleanDataCatalog, build_clean_dataset
from data.universe import load_universe_dataset
from research.universe import build_universe_from_dataset


def _frame(
    symbol: str = "A", periods: int = 20, start: str = "2024-01-01"
) -> pd.DataFrame:
    rows = []
    for day in pd.date_range(start, periods=periods, freq="B"):
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


def test_build_clean_dataset_writes_parquet_and_metadata(tmp_path) -> None:
    catalog = CleanDataCatalog(tmp_path)
    frames = {"A": _frame("A"), "B": _frame("B", start="2024-02-01")}
    meta = build_clean_dataset(
        ["A", "B"], frames, source="yfinance", exchange="NSE", data_dir=tmp_path
    )
    assert meta["A"].rows == 20
    assert meta["A"].is_clean
    assert (tmp_path / "clean" / "yfinance" / "A.parquet").is_file()
    assert (tmp_path / "clean" / "yfinance" / "A.meta.json").is_file()
    assert meta["A"].fingerprint
    assert meta["A"].validated_at

    frame, loaded_meta = catalog.read_clean("A")
    assert len(frame) == 20
    assert loaded_meta is not None and loaded_meta.fingerprint == meta["A"].fingerprint


def test_clean_dataset_panel_and_duckdb(tmp_path) -> None:
    frames = {"A": _frame("A"), "B": _frame("B", start="2024-01-01")}
    build_clean_dataset(["A", "B"], frames, data_dir=tmp_path)
    catalog = CleanDataCatalog(tmp_path)
    panel = catalog.load_market_panel(["A", "B"])
    assert panel.shape[1] == 2
    assert panel.index.is_monotonic_increasing
    assert catalog.dataset_fingerprint(["A", "B"])
    assert catalog.query("SELECT count(*) AS n FROM clean_market_data")["n"][0] == 40


def test_build_clean_dataset_rejects_dirty_data_when_required(tmp_path) -> None:
    frame = _frame()
    frame.loc[0, "high"] = 90.0  # high < max(open, close)
    with pytest.raises(ValueError, match="clean dataset rejected"):
        build_clean_dataset(["A"], {"A": frame}, data_dir=tmp_path, require_clean=True)


def test_dataset_pipeline_feeds_research_universe(tmp_path) -> None:
    """The clean dataset and the historical universe compose for research."""
    dataset = load_universe_dataset()
    universe = build_universe_from_dataset(dataset, "nifty100")
    symbols = list(universe.symbols)[:3]
    frames = {symbol: _frame(symbol) for symbol in symbols}
    meta = build_clean_dataset(symbols, frames, data_dir=tmp_path)
    assert all(meta[s].rows == 20 for s in symbols)
