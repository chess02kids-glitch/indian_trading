"""v0.7 real-data pipeline tests (offline, deterministic fixtures).

Covers v0.7 §18 for the real-data connection milestone:

* adapter: source -> canonical mapping, malformed/duplicate rejection,
  timestamp + symbol normalisation, provenance determinism, as-of
  future-date control (including end-to-end through the ingestion audit);
* quality gate: invalid OHLC reported-not-repaired, missing candles,
  staleness, duplicates end-to-end, accepted-frame alignment regression;
* universe: constituent validation (schema, dummy exclusion, ISIN join,
  invalid windows), missing-constituent handling, version fingerprinting
  and point-in-time membership boundaries;
* corporate actions: adjustment-state provenance + in-window check;
* pipeline: deterministic snapshot (byte-identical re-ingestion), frozen
  baseline configuration drift detection, point-in-time mask semantics,
  ingestion -> research integration;
* end-to-end: full local ingestion + the frozen v0.6 baseline on the
  fixture world -> gate -> shared ledger (ids continue, the v0.6 entry is
  never overwritten) + rerun reproducibility.

All fixtures are generated locally; no network access is required.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import importlib.util
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd
import pytest

from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig
from config.settings import settings
from data.dataset import CleanDataCatalog
from data.quality import (
    DataQualityError,
    check_ohlcv_long_frame,
    detect_data_staleness,
    detect_missing_candles,
)
from data.storage import StorageManager
from data.universe import UniverseDataset
from ingestion import eod2_adapter, nse_membership_adapter
from portfolio.construction import InverseVolatilityConstructor
from research import realdata
from research.contracts import Experiment, MarketData
from research.ledger import HypothesisLedger
from research.strategies import MomentumQualityStrategy

ROOT = Path(__file__).resolve().parent.parent

#: The locked v0.6 baseline values, restated independently here so a
#: silent edit of either side fails the test (v0.7 §1/§11).
EXPECTED_FROZEN: dict[str, Any] = {
    "momentum_lookback": 63,
    "momentum_quantile": 0.25,
    "quality_quantile": 0.5,
    "rebalance_frequency": "M",
    "initial_cash": 1_000_000.0,
    "cost_scenario": "base",
    "volatility_target": 0.15,
    "max_leverage": 1.0,
    "constructor_window": 20,
    "holdout_size": 252,
    "train_size": 252,
    "test_size": 63,
    "purge": 20,
    "embargo": 5,
    "cpcv_n_groups": 6,
    "cpcv_n_test_groups": 2,
    "acceptance_threshold": 0.5,
    "random_seed": 20260824,
}


# --------------------------------------------------------------------------
# fixture world
# --------------------------------------------------------------------------


_SCRIPT_CACHE: dict[str, Any] = {}


def _load_script(name: str) -> Any:
    if name not in _SCRIPT_CACHE:
        path = ROOT / "scripts" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"v07_script_{name}", path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        _SCRIPT_CACHE[name] = module
    return _SCRIPT_CACHE[name]


@contextlib.contextmanager
def _patched_data_dir(data_dir: Path) -> Iterator[None]:
    """Point the storage/clean layers at ``data_dir`` for the context.

    ``CleanDataCatalog`` reads ``settings.storage.data_dir`` at call time,
    while ``StorageManager`` binds its default at import time; both are
    patched (and restored by ``MonkeyPatch.undo``).
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(settings.storage, "data_dir", data_dir)
    mp.setattr(StorageManager.__init__, "__defaults__", (data_dir / "raw",))
    try:
        yield
    finally:
        mp.undo()


def make_mini_world(
    root: Path, *, days: int = 850, start: str = "2022-01-03", seed: int = 7
) -> dict[str, Any]:
    """Generate a small but structurally complete "real" data world.

    Layout (mirrors the pinned sources):

    * ``eod2/`` — ``daily/<lowercase-symbol>.csv`` (eod2 header),
      ``meta.json``, ``isin_symbol_map.json``;
    * ``membership/index_history/data/index_membership_history.csv`` —
      CRLF upstream membership table (index 219 = "Nifty 100", 217 =
      "Nifty 50" subset, 221 = "Nifty 500" superset, plus a decoy
      index-218 row and DUMMY demerger rows);
    * ``bundle/`` — the operator fundamentals bundle
      (``fundamentals_quarterly.parquet`` + provenance JSON).

    Population:

    * 50 complete symbols (``SYM00``..``SYM49``);
    * ``SPINOFF`` — left the index mid-window but stayed listed (full
      prices, finite ``valid_to``);
    * ``OLDBANK`` — delisted at a merger (finite ``valid_to``, notes carry
      the closure marker, **no price file** in the source);
    * ``NEWCO`` — IPO inside the window (prices start late -> incomplete
      history, member a few days after listing);
    * ``DUMMYVEDL1`` — non-tradeable demerger dummy (excluded, reported).
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=days)
    window_start = pd.Timestamp("2023-01-02")
    as_of = dates[-1]
    panel_days = int((dates >= window_start).sum())

    eod2_dir = root / "eod2"
    daily_dir = eod2_dir / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    membership_dir = root / "membership"
    csv_dir = membership_dir / "index_history" / "data"
    csv_dir.mkdir(parents=True, exist_ok=True)
    bundle_dir = root / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    complete_symbols = [f"SYM{i:02d}" for i in range(50)]
    spinoff_exit = dates[days // 2].date()
    newco_first = dates[2 * days // 3]
    newco_member_from = (newco_first + pd.Timedelta(days=5)).date()
    price_symbols = complete_symbols + ["SPINOFF", "NEWCO"]
    isin_map = {
        sym: f"INE{i:08d}1" for i, sym in enumerate(price_symbols + ["OLDBANK"])
    }

    def write_prices(symbol: str, first_idx: int = 0) -> None:
        n = len(dates)
        rets = rng.normal(0.0004, 0.012, size=n)
        close = 100.0 * np.cumprod(1.0 + rets)
        open_ = np.empty(n)
        open_[0] = close[0]
        open_[1:] = close[:-1]
        hi = np.maximum(open_, close) * (1.0 + rng.uniform(0.0005, 0.004, size=n))
        lo = np.minimum(open_, close) * (1.0 - rng.uniform(0.0005, 0.004, size=n))
        vol = rng.integers(100_000, 9_000_000, size=n)
        lines = [",".join(eod2_adapter.EOD2_DAILY_HEADER)]
        for i in range(first_idx, n):
            d = dates[i].strftime("%Y-%m-%d")
            lines.append(
                f"{d},{open_[i]:.2f},{hi[i]:.2f},{lo[i]:.2f},{close[i]:.2f},"
                f"{vol[i]},EQ,{int(vol[i] * 8)},{25},{int(vol[i] * 0.02)}"
            )
        (daily_dir / eod2_adapter.symbol_to_filename(symbol)).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    for symbol in complete_symbols + ["SPINOFF"]:
        write_prices(symbol)
    write_prices("NEWCO", first_idx=2 * days // 3)

    (eod2_dir / "meta.json").write_text(
        json.dumps(
            {
                "data-version": "3.4",
                "lastUpdate": f"{as_of.strftime('%Y-%m-%d')}T00:00:00+05:30",
                "equityActionsExpiry": (as_of + pd.Timedelta(days=3)).strftime(
                    "%Y-%m-%d"
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (eod2_dir / "isin_symbol_map.json").write_text(
        json.dumps({"sym2isin": isin_map}, indent=2), encoding="utf-8"
    )

    rows = [
        "index_id,index_name,symbol,valid_from,valid_to,"
        "weightage,source,source_url,notes"
    ]
    # Decoy row for a different index: must be filtered by index id.
    rows.append(
        '218,"Nifty 50",SYM00,2022-01-01,,1.0,snapshot_floor,,"decoy index row"'
    )
    for symbol in complete_symbols:
        rows.append(f'219,"Nifty 100",{symbol},2022-01-01,,1.0,snapshot_floor,,')
    rows.append(
        f'219,"Nifty 100",SPINOFF,2022-01-01,{spinoff_exit},'
        "1.0,press_release,https://nse.example/ind_prs01.pdf,"
    )
    rows.append(
        f'219,"Nifty 100",OLDBANK,2022-01-01,{spinoff_exit},'
        "1.0,press_release,https://nse.example/ind_prs02.pdf,"
        "closed by ind_prs04072023.pdf"
    )
    rows.append(
        f'219,"Nifty 100",NEWCO,{newco_member_from},'
        "1.0,press_release,https://nse.example/ind_prs03.pdf,"
    )
    rows.append('219,"Nifty 100",DUMMYVEDL1,2022-06-01,,1.0,snapshot,,demerger dummy')
    # The ingest script processes all three NSE indices (217/219/221), so the
    # world must carry valid rows for each: Nifty 50 is a subset of Nifty 100,
    # Nifty 500 a superset (same special members).
    for symbol in complete_symbols[:25]:
        rows.append(f'217,"Nifty 50",{symbol},2022-01-01,,1.0,snapshot_floor,,')
    for symbol in complete_symbols:
        rows.append(f'221,"Nifty 500",{symbol},2022-01-01,,1.0,snapshot_floor,,')
    rows.append(
        f'221,"Nifty 500",SPINOFF,2022-01-01,{spinoff_exit},'
        "1.0,press_release,https://nse.example/ind_prs01.pdf,"
    )
    rows.append(
        f'221,"Nifty 500",OLDBANK,2022-01-01,{spinoff_exit},'
        "1.0,press_release,https://nse.example/ind_prs02.pdf,"
        "closed by ind_prs04072023.pdf"
    )
    rows.append(
        f'221,"Nifty 500",NEWCO,{newco_member_from},'
        "1.0,press_release,https://nse.example/ind_prs03.pdf,"
    )
    rows.append('221,"Nifty 500",DUMMYVEDL1,2022-06-01,,1.0,snapshot,,demerger dummy')
    (csv_dir / "index_membership_history.csv").write_text(
        "\r\n".join(rows) + "\r\n", encoding="utf-8"
    )

    # Fundamentals bundle: quarter-end observations with the conservative
    # next-quarter-end availability date computed by the operator command's
    # own helper (midnight of the next quarter end; the final quarter's
    # rows become available after ``as_of`` and must be dropped, not used,
    # by the loader).
    next_quarter_end = _load_script("ingest_real_data")._next_quarter_end
    quarter_ends = list(pd.date_range(window_start, as_of, freq="QE"))
    fund_rows: list[dict[str, Any]] = []
    for quarter_end in quarter_ends:
        availability = next_quarter_end(quarter_end)
        for symbol in price_symbols:
            fund_rows.append(
                {
                    "date": availability,
                    "symbol": symbol,
                    "roe": round(float(rng.normal(0.12, 0.06)), 6),
                    "debt_to_equity": round(float(abs(rng.normal(0.8, 0.35))), 6),
                    "fiscal_quarter_end": quarter_end.date().isoformat(),
                    "source": "yfinance",
                    "fetched_at": "2025-06-01T00:00:00+00:00",
                }
            )
    bundle = pd.DataFrame(fund_rows)
    parquet_path = bundle_dir / "fundamentals_quarterly.parquet"
    bundle.to_parquet(parquet_path, index=False)
    (bundle_dir / "fundamentals_provenance.json").write_text(
        json.dumps(
            {
                "fetched_at": "2025-06-01T00:00:00+00:00",
                "yfinance_version": "0.2.40",
                "pandas_version": pd.__version__,
                "symbols_requested": len(price_symbols),
                "symbols_ok": len(price_symbols),
                "rows": int(len(bundle)),
                "bundle_fingerprint": hashlib.sha256(
                    parquet_path.read_bytes()
                ).hexdigest(),
                "availability_rule": "next quarter end (conservative publication lag)",
                "per_symbol": {s: {"status": "ok"} for s in price_symbols},
                "warnings": [],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return {
        "root": root,
        "eod2": eod2_dir,
        "membership": membership_dir,
        "bundle": bundle_dir,
        "dates": dates,
        "window_start": str(window_start.date()),
        "as_of": str(as_of.date()),
        "panel_days": panel_days,
        "spinoff_exit": spinoff_exit,
        "newco_member_from": newco_member_from,
        "complete_symbols": complete_symbols,
        "price_symbols": price_symbols,
        "isin_map": isin_map,
        "quarter_ends": quarter_ends,
    }


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> dict[str, Any]:
    return make_mini_world(tmp_path_factory.mktemp("v07_world"))


@pytest.fixture(scope="module")
def ingest_module() -> Any:
    return _load_script("ingest_real_data")


@pytest.fixture(scope="module")
def run_module() -> Any:
    return _load_script("run_real_data_experiment")


def _local_args(world: dict[str, Any], out_base: Path) -> list[str]:
    return [
        "--local",
        "--eod2-dir",
        str(world["eod2"]),
        "--membership-dir",
        str(world["membership"]),
        "--universe-root",
        str(out_base / "universe"),
        "--report-dir",
        str(out_base / "reports"),
        "--as-of",
        world["as_of"],
        "--window-start",
        world["window_start"],
    ]


@pytest.fixture(scope="module")
def ingested_world(world: dict[str, Any], ingest_module: Any) -> dict[str, Any]:
    """Run the offline ingestion once over the fixture world."""
    out_base = world["root"] / "ingested"
    data_dir = world["root"] / "ingested_data"
    with _patched_data_dir(data_dir):
        rc = ingest_module.main(_local_args(world, out_base))
    assert rc == 0
    world.update(
        {
            "data_dir": data_dir,
            "universe_dir": out_base / "universe",
            "report_dir": out_base / "reports",
            "report": json.loads(
                (out_base / "reports" / "completeness_report.json").read_text(
                    encoding="utf-8"
                )
            ),
        }
    )
    return world


def _write_eod2_csv(directory: Path, symbol: str, rows: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / eod2_adapter.symbol_to_filename(symbol)
    path.write_text(
        ",".join(eod2_adapter.EOD2_DAILY_HEADER) + "\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


def _spec() -> eod2_adapter.Eod2SourceSpec:
    return eod2_adapter.Eod2SourceSpec(
        commit="abc123def456",
        data_version="3.4",
        last_update="2024-01-04T00:00:00+05:30",
    )


# --------------------------------------------------------------------------
# adapter (v0.7 §18: source -> canonical mapping, malformed rejection,
# duplicates, missing fields, timestamp/symbol normalisation, provenance)
# --------------------------------------------------------------------------


def test_adapter_source_to_canonical_mapping(tmp_path: Path) -> None:
    rows = [
        "2024-01-02,100.00,101.00,99.50,100.50,123456,EQ,1000,123,500",
        "2024-01-03,100.50,102.00,100.00,101.75,234567,EQ,1100,213,600",
    ]
    path = _write_eod2_csv(tmp_path / "daily", "TESTCO", rows)
    frame = eod2_adapter.parse_eod2_daily_file(path, "testco", spec=_spec())

    expected = {
        "date",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source",
        "exchange",
        "ingested_at",
        "source_ts",
        "adjustment_state",
    }
    assert expected <= set(frame.columns)
    assert list(frame["symbol"]) == ["TESTCO", "TESTCO"]
    assert frame["date"].tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert frame["close"].tolist() == [100.50, 101.75]
    assert frame["volume"].tolist() == [123456, 234567]
    assert (frame["source"] == "eod2_data").all()
    assert (frame["exchange"] == "NSE").all()
    assert (frame["ingested_at"] == "2024-01-04T00:00:00+05:30").all()
    assert (frame["adjustment_state"] == "split_bonus_adjusted").all()
    assert (frame["source_ts"] == frame["date"].dt.strftime("%Y-%m-%d")).all()


def test_adapter_malformed_row_rejected(tmp_path: Path) -> None:
    rows = [
        "2024-01-02,100.00,101.00,99.50,100.50,1000,EQ,10,100,50",
        # high below low (and below close): inconsistent OHLC
        "2024-01-03,100.00,99.00,101.00,100.50,1000,EQ,10,100,50",
        "2024-01-04,100.00,101.00,99.50,100.50,1000,EQ,10,100,50",
    ]
    daily = tmp_path / "daily"
    _write_eod2_csv(daily, "BADCO", rows)
    accepted, report, stats = eod2_adapter.load_eod2_symbol(
        daily, "BADCO", spec=_spec()
    )
    assert report.total_rows == 3
    assert report.accepted_rows == 2
    kinds = {issue.kind for issue in report.issues}
    assert "ohlc_inconsistency" in kinds
    assert [d.date() for d in accepted["date"]] == [date(2024, 1, 2), date(2024, 1, 4)]
    assert stats.first_date == "2024-01-02"
    assert stats.last_date == "2024-01-04"
    assert stats.rows == 3


def test_adapter_duplicate_rows_rejected(tmp_path: Path) -> None:
    rows = [
        "2024-01-02,100.00,101.00,99.50,100.50,1000,EQ,10,100,50",
        # exact same (date, symbol) again: duplicate
        "2024-01-02,100.00,101.00,99.50,100.50,1000,EQ,10,100,50",
        "2024-01-03,100.50,102.00,100.00,101.75,1000,EQ,10,100,50",
    ]
    daily = tmp_path / "daily"
    _write_eod2_csv(daily, "DUPCO", rows)
    accepted, report, _ = eod2_adapter.load_eod2_symbol(daily, "DUPCO", spec=_spec())
    kinds = {issue.kind for issue in report.issues}
    assert "duplicate_row" in kinds
    assert report.accepted_rows == 1
    assert accepted["close"].tolist() == [101.75]


def test_adapter_missing_field_raises(tmp_path: Path) -> None:
    # Header without the Volume column: the strict header check refuses.
    path = _write_eod2_csv(
        tmp_path / "daily",
        "NOVOL",
        [
            "2024-01-02,100.00,101.00,99.50,100.50,EQ,10,100,50",
        ],
    )
    # Rewrite with the broken header (missing one field).
    path.write_text(
        ",".join(eod2_adapter.EOD2_DAILY_HEADER[1:])
        + "\n"
        + "2024-01-02,100.00,101.00,99.50,100.50,EQ,10,100,50\n",
        encoding="utf-8",
    )
    with pytest.raises(DataQualityError, match="unexpected header"):
        eod2_adapter.parse_eod2_daily_file(path, "NOVOL", spec=_spec())


def test_adapter_timestamp_normalization(tmp_path: Path) -> None:
    rows = [
        "2024-01-02,100.00,101.00,99.50,100.50,1000,EQ,10,100,50",
        "not-a-date,100.00,101.00,99.50,100.50,1000,EQ,10,100,50",
        "2024-01-03,100.50,102.00,100.00,101.75,1000,EQ,10,100,50",
    ]
    daily = tmp_path / "daily"
    _write_eod2_csv(daily, "TSBAD", rows)
    frame = eod2_adapter.parse_eod2_daily_file(
        daily / "tsbad.csv", "TSBAD", spec=_spec()
    )
    # Parse normalises to datetime; unparseable values become NaT.
    assert frame["date"].isna().tolist() == [False, True, False]
    accepted, report, _ = eod2_adapter.load_eod2_symbol(daily, "TSBAD", spec=_spec())
    kinds = {issue.kind for issue in report.issues}
    assert "invalid_timestamp" in kinds
    assert [d.date() for d in accepted["date"]] == [date(2024, 1, 2), date(2024, 1, 3)]


def test_symbol_normalization_and_symbol_discovery(tmp_path: Path) -> None:
    assert eod2_adapter.symbol_to_filename("M&M") == "m&m.csv"
    assert eod2_adapter.symbol_to_filename("  m&m ") == "m&m.csv"
    assert eod2_adapter.symbol_to_filename("TATA-POWER") == "tata-power.csv"
    with pytest.raises(ValueError):
        eod2_adapter.symbol_to_filename("BAD SYM")
    with pytest.raises(ValueError):
        eod2_adapter.symbol_to_filename("")

    daily = tmp_path / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    (daily / "m&m.csv").write_text("x\n", encoding="utf-8")
    (daily / "tata-power.csv").write_text("x\n", encoding="utf-8")
    assert eod2_adapter.eod2_symbols_available(tmp_path) == {"M&M", "TATA-POWER"}


def test_adapter_provenance_is_deterministic(tmp_path: Path) -> None:
    meta = {
        "data-version": "3.4",
        "lastUpdate": "2026-08-21T00:00:00+05:30",
        "equityActionsExpiry": "2026-08-28",
    }
    spec = eod2_adapter.Eod2SourceSpec.from_meta(
        meta, commit="b7b590a2d5f01b2b73417dc766369310a55f56de"
    )
    # ingested_at is the SOURCE data timestamp, never the wall clock.
    assert spec.ingested_at == "2026-08-21T00:00:00+05:30"
    as_dict = spec.to_dict()
    assert as_dict["source"] == "eod2_data"
    assert as_dict["commit"].startswith("b7b590a2d5f0")
    assert as_dict["adjustment_state"] == "split_bonus_adjusted"
    assert as_dict["data_version"] == "3.4"
    assert as_dict["equity_actions_window"] == "2026-08-28"

    rows = ["2024-01-02,100.00,101.00,99.50,100.50,1000,EQ,10,100,50"]
    daily = tmp_path / "daily"
    _write_eod2_csv(daily, "PROMA", rows)
    path = daily / "proma.csv"
    first = eod2_adapter.parse_eod2_daily_file(path, "PROMA", spec=spec)
    second = eod2_adapter.parse_eod2_daily_file(path, "PROMA", spec=spec)
    assert first["ingested_at"].tolist() == second["ingested_at"].tolist()
    assert (first["ingested_at"] == spec.ingested_at).all()


def test_adapter_as_of_future_date_rejection(tmp_path: Path) -> None:
    rows = [
        f"2024-01-0{day},100.00,101.00,99.50,100.50,1000,EQ,10,100,50"
        for day in range(2, 7)
    ]
    daily = tmp_path / "daily"
    _write_eod2_csv(daily, "FUTCO", rows)
    accepted, report, _ = eod2_adapter.load_eod2_symbol(
        daily, "FUTCO", spec=_spec(), as_of="2024-01-04"
    )
    future = [issue for issue in report.issues if issue.kind == "future_date"]
    assert {issue.date for issue in future} == {"2024-01-05", "2024-01-06"}
    assert pd.to_datetime(accepted["date"]).max().date() == date(2024, 1, 4)


# --------------------------------------------------------------------------
# quality gate (v0.7 §6/§18: report, never silently repair)
# --------------------------------------------------------------------------


def test_quality_future_date_end_to_end(
    world: dict[str, Any], ingest_module: Any, tmp_path: Path
) -> None:
    """Ingestion with a mid-history as-of excludes + counts future rows."""
    eod2_dir = world["eod2"]
    symbol = "SYM00"
    spec = eod2_adapter.Eod2SourceSpec.from_meta(
        eod2_adapter.load_meta_json(eod2_dir), commit="test"
    )
    path = eod2_dir / "daily" / eod2_adapter.symbol_to_filename(symbol)
    pre_parsed = {symbol: eod2_adapter.parse_eod2_daily_file(path, symbol, spec=spec)}
    as_of = str(world["dates"][600].date())
    data_dir = tmp_path / "data"
    with _patched_data_dir(data_dir):
        catalog = CleanDataCatalog()
        audit = ingest_module.ingest_prices(
            pre_parsed,
            eod2_dir=eod2_dir,
            symbols=[symbol],
            as_of=as_of,
            window_start=world["window_start"],
            window_end=as_of,
            catalog=catalog,
            storage=StorageManager(),
        )
    counts = audit["combined"]["quality_issue_counts"]
    assert counts.get("future_date", 0) > 0
    frame, _ = catalog.read_clean(symbol, source="eod2_data")
    assert pd.to_datetime(frame["date"]).max().date() <= date.fromisoformat(as_of)
    # Raw layer is window-scoped and present.
    raw_files = list(
        (data_dir / "raw" / "eod2_data" / "NSE" / symbol).rglob("*.parquet")
    )
    assert raw_files


def test_invalid_ohlc_rejected_not_repaired() -> None:
    dates = pd.bdate_range("2024-01-01", periods=5)
    frame = pd.DataFrame(
        {
            "date": dates,
            "symbol": ["AAA"] * 5,
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.5] * 5,
            "volume": [1000] * 5,
        }
    )
    frame.loc[1, "close"] = -5.0  # non-positive price
    frame.loc[3, "high"] = 98.0  # high < low
    accepted, report = check_ohlcv_long_frame(frame, source="t", exchange="NSE")
    assert report.total_rows == 5
    assert report.accepted_rows == 3
    kinds = {issue.kind for issue in report.issues}
    assert {"invalid_close", "ohlc_inconsistency"} <= kinds
    # No imputation: accepted rows are exactly the untouched valid rows.
    kept = frame.loc[[0, 2, 4]].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        accepted[
            ["date", "symbol", "open", "high", "low", "close", "volume"]
        ].reset_index(drop=True),
        kept[["date", "symbol", "open", "high", "low", "close", "volume"]],
    )


def test_missing_candle_detection() -> None:
    dates = pd.bdate_range("2024-01-01", periods=10)
    aaa_dates = list(dates)
    bbb_dates = list(dates[:9])  # BBB is missing the final day (2024-01-12)
    frame = pd.DataFrame(
        {
            "date": aaa_dates + bbb_dates,
            "symbol": ["AAA"] * 10 + ["BBB"] * 9,
            "open": [100.0] * 19,
            "high": [101.0] * 19,
            "low": [99.0] * 19,
            "close": [100.5] * 19,
            "volume": [1000] * 19,
        }
    )
    # BBB missing a date that AAA has is a gap; dates before a symbol's
    # first candle are "not yet listed", not gaps.
    issues = detect_missing_candles(frame)
    missing = [i for i in issues if i.kind == "missing_candle"]
    assert [(i.symbol, i.date) for i in missing] == [("BBB", "2024-01-12")]


def test_staleness_detection() -> None:
    index = pd.DatetimeIndex([pd.Timestamp("2026-08-20"), pd.Timestamp("2026-08-21")])
    fresh = detect_data_staleness(
        index, reference_now=datetime(2026, 8, 22, 12, 0), max_staleness_days=6.0
    )
    assert fresh is None
    stale = detect_data_staleness(
        index, reference_now=datetime(2026, 9, 5, 12, 0), max_staleness_days=6.0
    )
    assert stale is not None
    assert stale.kind == "staleness"
    assert "2026-08-21" in stale.detail


def test_duplicate_end_to_end_clean_layer(tmp_path: Path) -> None:
    catalog = CleanDataCatalog(tmp_path / "data")
    dates = pd.bdate_range("2024-01-01", periods=4)
    frame = pd.DataFrame(
        {
            "date": [dates[0], dates[0], dates[1], dates[2], dates[3]],
            "symbol": ["AAA"] * 5,
            "open": [100.0] * 5,
            "high": [101.0] * 5,
            "low": [99.0] * 5,
            "close": [100.5] * 5,
            "volume": [1000] * 5,
        }
    )
    frame.loc[1, "close"] = 100.7  # same date, different close: still a dup
    path, metadata = catalog.write_clean(
        frame, source="eod2_data", exchange="NSE", symbol="AAA"
    )
    # Both rows of the duplicate pair are excluded (never "keep first").
    assert metadata.rows == 3
    assert "duplicate_row" in metadata.quality_issues
    assert not metadata.is_clean
    on_disk = pd.read_parquet(path)
    assert len(on_disk) == 3
    assert on_disk["date"].isna().sum() == 0


def test_accepted_frame_alignment_regression() -> None:
    """Regression: rejected rows must never shift the accepted columns.

    The v0.6 ``check_ohlcv_long_frame`` re-attached symbol/date columns
    *after* ``reset_index`` using the *original* index, so any rejection
    below the accepted row count silently misaligned the accepted frame
    (observed on a real symbol during v0.7 ingestion). Rejected rows here
    sit at original positions 2 and 5, both below the kept row count.
    """
    dates = pd.bdate_range("2024-01-01", periods=10)
    frame = pd.DataFrame(
        {
            "date": np.repeat(dates, 2),
            "symbol": np.tile(["AAA", "BBB"], 10),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1000,
            "source_ts": [d.strftime("%Y-%m-%d") for d in np.repeat(dates, 2)],
        }
    )
    frame.loc[2, "close"] = 0.0  # (2024-01-02, AAA): invalid close
    frame.loc[5, "high"] = 98.0  # (2024-01-03, BBB): high < low
    accepted, report = check_ohlcv_long_frame(frame, source="t", exchange="NSE")
    kinds = {issue.kind for issue in report.issues}
    assert {"invalid_close", "ohlc_inconsistency"} <= kinds
    kept = frame.loc[[i for i in range(len(frame)) if i not in (2, 5)]]
    assert list(accepted["date"]) == list(kept["date"])
    assert list(accepted["symbol"]) == list(kept["symbol"])
    assert list(accepted["source_ts"]) == list(kept["source_ts"])


# --------------------------------------------------------------------------
# universe (v0.7 §8/§18: PIT membership, validation, fingerprinting)
# --------------------------------------------------------------------------


def test_universe_constituent_validation(
    world: dict[str, Any], ingested_world: dict[str, Any]
) -> None:
    csv_path = (
        world["membership"] / "index_history" / "data" / "index_membership_history.csv"
    )
    frame = nse_membership_adapter.parse_membership_csv(csv_path)
    rows = nse_membership_adapter.extract_index_rows(
        frame,
        index_id=nse_membership_adapter.NIFTY_100_INDEX_ID,
        index_name="Nifty 100",
    )
    # Decoy row for the other index (218) is filtered out by index id:
    # 50 SYM + SPINOFF + OLDBANK + NEWCO + DUMMYVEDL1 = 54 rows.
    assert len(rows) == 54
    assert len(rows[rows["symbol"] == "SYM00"]) == 1
    pit = nse_membership_adapter.build_pit_universe_frame(
        rows, isin_map=world["isin_map"]
    )
    # DUMMY demerger rows are excluded from the universe but reported.
    assert "DUMMYVEDL1" not in set(pit["symbol"])
    assert set(pit["symbol"]) == set(world["price_symbols"] + ["OLDBANK"])
    assert pit.attrs["excluded_symbols"] == ["DUMMYVEDL1"]
    # ISIN join + delisted flag from the closure marker.
    assert (
        pit.loc[pit["symbol"] == "SYM00", "isin"].iloc[0] == world["isin_map"]["SYM00"]
    )
    assert pit.loc[pit["symbol"] == "OLDBANK", "delisted"].iloc[0] in (True, 1)
    assert pit.loc[pit["symbol"] == "SYM00", "delisted"].iloc[0] in (False, 0)
    # Open-ended membership round-trips through the repo universe contract.
    public = pit.drop(columns=[c for c in pit.columns if c.startswith("_")])
    dataset = UniverseDataset.from_frame(public)
    assert "SYM00" in dataset.members_at("nifty100", world["as_of"])
    # Invalid window is rejected by the repository contract.
    bad = public.copy()
    bad.loc[0, "valid_to"] = "2021-01-01"  # before valid_from
    with pytest.raises(ValueError, match="valid_to before valid_from"):
        UniverseDataset.from_frame(bad)
    # The committed PIT CSV loads unchanged through from_dir.
    disk_dataset = UniverseDataset.from_dir(ingested_world["universe_dir"] / "nifty100-pit")
    assert disk_dataset.all_symbols("nifty100") == dataset.all_symbols("nifty100")


def test_missing_constituent_handling(
    world: dict[str, Any], ingested_world: dict[str, Any]
) -> None:
    catalog = CleanDataCatalog(world["data_dir"])
    dataset = UniverseDataset.from_dir(world["universe_dir"] / "nifty100-pit")
    requested = realdata.requested_constituents(
        dataset, window_start=world["window_start"], as_of=world["as_of"]
    )
    # Members at any point in the window: 50 + SPINOFF + OLDBANK + NEWCO.
    assert set(requested) == set(world["price_symbols"] + ["OLDBANK"])
    panels = realdata.build_market_panels(
        catalog,
        requested,
        source="eod2_data",
        window_start=world["window_start"],
        window_end=world["as_of"],
    )
    # Excluded with explicit reasons, never silently dropped.
    assert "OLDBANK" in panels.excluded
    assert "no validated (clean) data" in panels.excluded["OLDBANK"]
    assert "NEWCO" in panels.excluded
    assert "incomplete price history" in panels.excluded["NEWCO"]
    # OLDBANK (no price file in the source) never enters the panel; NEWCO
    # keeps a place (its gaps are forward/back-filled) and is annotated,
    # never silently dropped.
    assert set(panels.symbols) == set(requested) - {"OLDBANK"}
    assert len(panels.symbols) == 52
    # The completeness report sharpens the source-level reason.
    report_excluded = world["report"]["panel"]["excluded_symbols"]
    assert "no price file" in report_excluded["OLDBANK"]
    assert "incomplete price history" in report_excluded["NEWCO"]


def test_universe_version_fingerprinting(
    world: dict[str, Any], ingested_world: dict[str, Any]
) -> None:
    csv_path = world["universe_dir"] / "nifty100-pit" / "nifty100.csv"
    first = nse_membership_adapter.membership_fingerprint(pd.read_csv(csv_path))
    second = nse_membership_adapter.membership_fingerprint(pd.read_csv(csv_path))
    assert first == second
    assert len(first) == 64
    changed = pd.read_csv(csv_path)
    changed.loc[0, "valid_from"] = "2022-02-01"
    assert nse_membership_adapter.membership_fingerprint(changed) != first

    dataset = UniverseDataset.from_dir(world["universe_dir"] / "nifty100-pit")
    exit_day = world["spinoff_exit"]
    before = dataset.members_at("nifty100", exit_day - timedelta(days=1))
    on_exit = dataset.members_at("nifty100", exit_day)
    after = dataset.members_at("nifty100", exit_day + timedelta(days=1))
    # SPINOFF and OLDBANK are members until (and on) their valid_to day.
    assert "SPINOFF" in before and "OLDBANK" in before
    assert "SPINOFF" in on_exit and "OLDBANK" in on_exit
    assert "SPINOFF" not in after and "OLDBANK" not in after
    # NEWCO joins a few days after listing, not before.
    join_day = world["newco_member_from"]
    assert "NEWCO" not in dataset.members_at("nifty100", join_day - timedelta(days=1))
    assert "NEWCO" in dataset.members_at("nifty100", join_day)
    # Members at as-of: the 50 permanent names + NEWCO.
    assert len(dataset.members_at("nifty100", world["as_of"])) == 51


def test_adjustment_state_validation(
    world: dict[str, Any], ingested_world: dict[str, Any]
) -> None:
    eod2_dir = world["eod2"]
    spec = eod2_adapter.Eod2SourceSpec.from_meta(
        eod2_adapter.load_meta_json(eod2_dir), commit="test"
    )
    path = eod2_dir / "daily" / "sym00.csv"
    frame = eod2_adapter.parse_eod2_daily_file(path, "SYM00", spec=spec)
    # Every normalised row carries the adjustment state + source provenance.
    assert (frame["adjustment_state"] == "split_bonus_adjusted").all()
    assert (frame["source"] == "eod2_data").all()
    assert (frame["exchange"] == "NSE").all()

    report = world["report"]
    adjustment = report["adjustment"]
    assert adjustment["state"] == "split_bonus_adjusted"
    assert "dividends are NOT adjusted" in adjustment["note"]
    # The fixture has no OHLC inconsistencies anywhere -> verified clean.
    assert adjustment["ohlc_inconsistent_row_count"] == 0
    assert (
        "verified: zero OHLC-inconsistent rows inside the research window"
        in adjustment["ohlc_inconsistent_window_check"]
    )
    assert report["prices"]["source"]["adjustment_state"] == "split_bonus_adjusted"


def test_symbol_continuity_isin(
    world: dict[str, Any], ingested_world: dict[str, Any]
) -> None:
    """Delisted names keep their own ISIN; continuity is per-instrument.

    (Mirrors the real HDFC vs HDFCBANK case: distinct ISINs, the delisted
    name is flagged, the remaining name stays a member throughout.)
    """
    dataset = UniverseDataset.from_dir(world["universe_dir"] / "nifty100-pit")
    old_bank = [m for m in dataset.for_index("nifty100") if m.symbol == "OLDBANK"]
    assert len(old_bank) == 1
    member = old_bank[0]
    assert member.isin == world["isin_map"]["OLDBANK"]
    assert member.delisted is True
    assert member.valid_to == world["spinoff_exit"]
    # Distinct instrument: the ISIN differs from any continuing member.
    continuing = {m.isin for m in dataset.for_index("nifty100") if not m.delisted}
    assert member.isin not in continuing
    report = world["report"]
    assert report["universe"]["isin_coverage"]["unmapped"] == 0


# --------------------------------------------------------------------------
# pipeline (v0.7 §14/§18: deterministic snapshot, frozen config, holdout
# isolation via the PIT mask, ingestion -> research integration)
# --------------------------------------------------------------------------


def _capture_local_run(
    world: dict[str, Any], base: Path, ingest_module: Any
) -> dict[str, Any]:
    data_dir = base / "data"
    out_dir = base / "out"
    with _patched_data_dir(data_dir):
        rc = ingest_module.main(_local_args(world, out_dir))
    assert rc == 0
    clean_dir = data_dir / "clean" / "eod2_data"
    clean_sha: dict[str, str] = {}
    clean_fp: dict[str, str] = {}
    for path in sorted(clean_dir.glob("*.parquet")):
        meta = json.loads(
            (clean_dir / f"{path.stem}.meta.json").read_text(encoding="utf-8")
        )
        clean_sha[path.stem] = hashlib.sha256(path.read_bytes()).hexdigest()
        clean_fp[path.stem] = meta["fingerprint"]
    report = json.loads(
        (out_dir / "reports" / "completeness_report.json").read_text(encoding="utf-8")
    )
    return {
        "clean_sha": clean_sha,
        "clean_fp": clean_fp,
        "report": report,
        "universe_csv": (out_dir / "universe" / "nifty100-pit" / "nifty100.csv").read_bytes(),
        "panel_symbols": (out_dir / "universe" / "panel_symbols.txt").read_text(
            encoding="utf-8"
        ),
    }


def test_deterministic_snapshot(
    world: dict[str, Any], ingest_module: Any, tmp_path: Path
) -> None:
    """Two fresh local ingestions of the same pinned world must be
    byte-for-byte identical (v0.7 §14: re-run detects dataset changes)."""
    base = tmp_path / "det"
    first = _capture_local_run(world, base, ingest_module)
    # Wipe every generated artifact, keep the (absolute) paths stable.
    shutil.rmtree(base / "data")
    shutil.rmtree(base / "out")
    second = _capture_local_run(world, base, ingest_module)

    assert set(first["clean_sha"]) == set(second["clean_sha"])
    assert len(first["clean_sha"]) == 52  # every price file, incl. NEWCO
    for symbol in first["clean_sha"]:
        assert first["clean_sha"][symbol] == second["clean_sha"][symbol], symbol
        assert first["clean_fp"][symbol] == second["clean_fp"][symbol], symbol
    assert first["universe_csv"] == second["universe_csv"]
    assert first["panel_symbols"] == second["panel_symbols"]
    # Identical §7 report (the staleness detail is wall-clock relative).
    staleness = first["report"].pop("staleness", None)
    second["report"].pop("staleness", None)
    assert first["report"] == second["report"]
    # The fixture world is old by construction: staleness is reported.
    assert staleness is not None and staleness["kind"] == "staleness"


def test_frozen_baseline_config_and_drift_detection(
    run_module: Any, tmp_path: Path
) -> None:
    assert run_module.FROZEN == EXPECTED_FROZEN

    engine_config = BacktestConfig(
        rebalance_frequency="M",
        initial_cash=1_000_000.0,
        cost_model=IndiaCostModel(scenario="base"),
        volatility_target=0.15,
        use_vectorbt=False,
    )
    strategy = MomentumQualityStrategy(
        momentum_lookback=63, momentum_quantile=0.25, quality_quantile=0.5
    )
    constructor = InverseVolatilityConstructor(window=20)
    # The frozen values pass the assertion...
    run_module._assert_frozen_config(engine_config, strategy, constructor)
    # ...and any drift aborts the experiment.
    drifted = dataclasses.replace(strategy, momentum_lookback=64)
    with pytest.raises(SystemExit, match="drifted"):
        run_module._assert_frozen_config(engine_config, drifted, constructor)
    drifted_cost = BacktestConfig(
        rebalance_frequency="M",
        initial_cash=1_000_000.0,
        cost_model=IndiaCostModel(scenario="pessimistic"),
        volatility_target=0.15,
        use_vectorbt=False,
    )
    with pytest.raises(SystemExit, match="drifted"):
        run_module._assert_frozen_config(drifted_cost, strategy, constructor)
    # CLI guards on the locked holdout / seed values.
    with pytest.raises(SystemExit, match="holdout-size"):
        run_module.main(["--holdout-size", "251", "--output-dir", str(tmp_path)])
    with pytest.raises(SystemExit, match="seed"):
        run_module.main(["--seed", "1", "--output-dir", str(tmp_path)])


def test_pit_membership_mask_semantics() -> None:
    """The cross-sectional screens rank ONLY within each date's members.

    C has the strongest momentum but is a member from day 12 onward. With a
    top-10% momentum cut: before day 12 the strongest *member* (D) must be
    selected (a rank-then-mask implementation would select C, mask it to
    nothing, and silently change the cross-section); after day 12 C is
    selected and D drops out of the top cut.
    """
    index = pd.bdate_range("2024-01-02", periods=70)
    rates = {"AAA": -0.10, "BBB": -0.05, "CCC": 0.30, "DDD": 0.10}
    closes = {}
    for symbol, rate in rates.items():
        series = np.full(70, 100.0)
        series[6:] = 100.0 * (1.0 + rate) ** np.arange(1, 65)
        closes[symbol] = series
    close = pd.DataFrame(closes, index=index)
    fundamentals = pd.DataFrame(
        [
            ("2024-01-02", "AAA", 0.20, 0.20),
            ("2024-01-02", "BBB", 0.15, 0.40),
            ("2024-01-02", "CCC", 0.05, 0.90),
            ("2024-01-02", "DDD", 0.10, 0.60),
        ],
        columns=["date", "symbol", "roe", "debt_to_equity"],
    )
    mask = pd.DataFrame(False, index=index, columns=list(rates))
    mask[["AAA", "BBB", "DDD"]] = True
    mask.loc[index[12:], "CCC"] = True

    def signals_for(members: pd.DataFrame) -> np.ndarray:
        strategy = MomentumQualityStrategy(
            momentum_lookback=5,
            momentum_quantile=0.1,
            quality_quantile=1.0,
            fundamentals=fundamentals,
            active_members=members,
        )
        return strategy.generate_signals(MarketData(close=close)).values

    values = signals_for(mask)
    loc_a = values.columns.get_loc("AAA")
    loc_c = values.columns.get_loc("CCC")
    loc_d = values.columns.get_loc("DDD")
    # Before C joins: the strongest member D is selected; C (non-member)
    # is never selected even though it has the best panel momentum.
    assert (values.iloc[7:12, loc_d] > 0).all()
    assert (values.iloc[7:12, loc_a] == 0).all()
    assert (values.iloc[7:12, loc_c] == 0).all()
    # After C joins: C is the top momentum member and wins the cut; D drops.
    assert (values.iloc[12:, loc_c] > 0).all()
    assert (values.iloc[12:, loc_a] == 0).all()
    assert (values.iloc[12:, loc_d] == 0).all()
    # An incomplete mask is conservative: an unlisted date selects nothing.
    hole = mask.copy()
    hole.iloc[30, :] = False
    holed = signals_for(hole)
    assert (holed.iloc[30] == 0).all()
    # Default (no mask) keeps the frozen v0.6 behaviour: every column ranks,
    # so the strongest momentum (C) is selected even without membership.
    unmasked = MomentumQualityStrategy(
        momentum_lookback=5,
        momentum_quantile=0.1,
        quality_quantile=1.0,
        fundamentals=fundamentals,
    ).generate_signals(MarketData(close=close))
    assert unmasked.metadata["active_members_mask"] is False
    assert unmasked.values.iloc[30, loc_c] > 0


def test_ingestion_to_research_integration(
    world: dict[str, Any], ingested_world: dict[str, Any]
) -> None:
    """Ingestion -> clean layer -> panels -> PIT mask -> strategy signals."""
    catalog = CleanDataCatalog(world["data_dir"])
    dataset = UniverseDataset.from_dir(world["universe_dir"] / "nifty100-pit")
    requested = realdata.requested_constituents(
        dataset, window_start=world["window_start"], as_of=world["as_of"]
    )
    panels = realdata.build_market_panels(
        catalog,
        requested,
        source="eod2_data",
        window_start=world["window_start"],
        window_end=world["as_of"],
    )
    assert panels.close.shape == (world["panel_days"], 52)
    assert not panels.close.isna().any().any()
    assert (panels.close > 0).all().all()

    mask = realdata.build_active_membership_panel(
        dataset,
        "nifty100",
        calendar=panels.window.index,
        symbols=panels.symbols,
    )
    assert mask.shape == panels.close.shape
    assert mask.dtypes.eq(bool).all()
    # SPINOFF is masked out exactly after its valid_to day.
    exit_pos = panels.close.index.get_loc(pd.Timestamp(world["spinoff_exit"]))
    assert bool(mask["SPINOFF"].iloc[exit_pos]) is True
    assert not mask["SPINOFF"].iloc[exit_pos + 1 :].any()

    fundamentals, provenance = realdata.load_fundamentals_bundle(
        world["bundle"], as_of=world["as_of"]
    )
    # The final quarter's availability (next quarter end) post-dates as-of
    # and is dropped, counted, never used.
    next_quarter_end = _load_script("ingest_real_data")._next_quarter_end
    dropped_quarters = sum(
        1
        for quarter_end in world["quarter_ends"]
        if next_quarter_end(quarter_end) > pd.Timestamp(world["as_of"])
    )
    assert provenance["dropped_after_as_of"] == dropped_quarters * 52
    assert len(fundamentals) == len(world["price_symbols"]) * (
        len(world["quarter_ends"]) - dropped_quarters
    )

    strategy = MomentumQualityStrategy(fundamentals=fundamentals, active_members=mask)
    signal = strategy.generate_signals(panels.market_data)
    assert signal.values.shape == panels.close.shape
    assert np.isfinite(signal.values.to_numpy()).all()
    assert (signal.values >= 0).all().all()
    assert bool((signal.values > 0).to_numpy().any())


# --------------------------------------------------------------------------
# end-to-end (v0.7 §18/§20/§21: ingestion -> backtest -> gate -> ledger,
# frozen baseline, reproducibility)
# --------------------------------------------------------------------------

STABLE_SUMMARY_KEYS = (
    "strategy",
    "universe",
    "universe_kind",
    "universe_version",
    "dataset_version",
    "cost_model",
    "status",
    "reason",
    "full_period_metrics",
    "backtest_period",
    "dev_period",
    "holdout_period",
    "holdout_boundaries",
    "holdout_metrics",
    "cost_scenario_results",
    "holdout_benchmarks",
    "oos_period",
    "deflated_sharpe",
    "bootstrap_ci",
    "confidence_intervals",
    "walk_forward_folds",
    "cpcv_folds",
    "walk_forward_consistency",
    "cpcv_consistency",
    "comparison",
    "holdout_comparison",
    "warnings",
    "limitations",
    "frozen_config",
    "config_fingerprint",
    "panel_symbols",
    "excluded_symbols",
)


def _stable_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: summary[key] for key in STABLE_SUMMARY_KEYS}


def test_full_real_data_pipeline_ledger_and_reproducibility(
    world: dict[str, Any],
    ingested_world: dict[str, Any],
    run_module: Any,
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "exp"
    ledger_path = tmp_path / "ledger.jsonl"
    bundle_dir = world["bundle"]
    universe_dir = world["universe_dir"]

    # Seed the v0.6 entry so the real-data experiment continues the ledger
    # at HYP-00002 (v0.7 §21: never overwrite HYP-00001).
    seed = HypothesisLedger(ledger_path)
    seed_experiment = Experiment(
        hypothesis_id="HYP-00001",
        strategy="momentum_quality",
        parameters={
            "momentum_lookback": 63,
            "momentum_quantile": 0.25,
            "quality_quantile": 0.5,
            "fundamentals_rows": 0,
        },
        factor_set=["momentum_3m", "quality_composite"],
        universe="nifty100",
        dataset_version="synthetic-test",
        cost_model="india:base",
    )
    seed.for_experiment(
        seed_experiment,
        status="rejected",
        hypothesis_text="v0.6 synthetic baseline (fixture seed)",
        metrics={"score": 62.5},
        dataset_version="synthetic-test",
        dataset_fingerprint="test",
        config_fingerprint="test",
        code_fingerprint="test",
        run_id="local",
        gate_result={"verdict": "FAIL", "score": 62.5},
    )

    args = [
        "--output-dir",
        str(out_dir),
        "--universe-dir",
        str(universe_dir),
        "--bundle-dir",
        str(bundle_dir),
        "--ledger-path",
        str(ledger_path),
        "--as-of",
        world["as_of"],
        "--window-start",
        world["window_start"],
    ]
    # The fixture panel must be long enough for the frozen protocol
    # (dev prefix >= 252 train + 5 embargo + 63 test).
    assert world["panel_days"] - 252 >= 320

    with _patched_data_dir(world["data_dir"]):
        first_rc = run_module.main(args)
        first_summary = json.loads(
            (out_dir / "baseline_experiment_summary.json").read_text(encoding="utf-8")
        )
        second_rc = run_module.main(args)
        second_summary = json.loads(
            (out_dir / "baseline_experiment_summary.json").read_text(encoding="utf-8")
        )
    assert first_rc == 0
    assert second_rc == 0

    # -- frozen configuration actually used -------------------------------
    assert first_summary["frozen_config"] == EXPECTED_FROZEN
    assert first_summary["status"] in {"accepted", "rejected", "insufficient_data"}

    # -- panel / exclusions (v0.7 §7) --------------------------------------
    assert first_summary["panel_symbols"] == 52
    excluded = first_summary["excluded_symbols"]
    assert "OLDBANK" in excluded
    assert "NEWCO" in excluded
    assert "no validated (clean) data" in excluded["OLDBANK"]
    assert "incomplete price history" in excluded["NEWCO"]

    # -- versions and boundaries -------------------------------------------
    assert first_summary["dataset_version"].startswith("real-nifty100-v1")
    assert "nse-membership@" in first_summary["dataset_version"]
    assert first_summary["universe_version"].startswith("nifty100-pit-")
    assert first_summary["universe_kind"] == "point_in_time"
    assert (
        first_summary["holdout_boundaries"]["holdout_size"]
        == EXPECTED_FROZEN["holdout_size"]
    )

    # -- research gate -------------------------------------------------------
    gate = first_summary["research_gate"]
    assert gate["verdict"] in {
        "PASS",
        "FAIL",
        "FRAGILE",
        "INSUFFICIENT_EVIDENCE",
    }
    # DSR evidence is recorded and in the unit interval.
    assert 0.0 <= first_summary["deflated_sharpe"]["probability"] <= 1.0

    # -- ledger: ids continue, v0.6 entry untouched (v0.7 §21) --------------
    lines = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [line["hypothesis_id"] for line in lines] == [
        "HYP-00001",
        "HYP-00002",
        "HYP-00003",
    ]
    record = lines[1]
    assert lines[0]["dataset_version"] == "synthetic-test"  # untouched
    assert record["status"] == first_summary["status"]
    assert record["dataset_version"] == first_summary["dataset_version"]
    assert record["universe_version"] == first_summary["universe_version"]
    assert record["holdout_period"] == first_summary["holdout_period"]
    assert record["config_fingerprint"] == first_summary["config_fingerprint"]
    assert record["dataset_fingerprint"] == record["dataset_fingerprint"]
    assert record["gate_result"]["verdict"] == gate["verdict"]
    assert record["cost_model"] == "india:base"

    # -- gate -> ledger -> summary consistency -------------------------------
    if first_summary["reason"] is not None:
        assert record["reason"] == first_summary["reason"]

    # -- reproducibility: identical research result on re-run ----------------
    assert _stable_summary(first_summary) == _stable_summary(second_summary)
    assert first_summary["hypothesis_id"] == "HYP-00002"
    assert second_summary["hypothesis_id"] == "HYP-00003"
