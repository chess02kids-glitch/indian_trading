"""Validated research dataset builder over raw Parquet + DuckDB.

Raw ingestion is owned by ``ingestion.pipeline`` / ``data.storage`` and is
treated as immutable. This module sits *above* raw storage and produces the
validated **clean** layer used by research:

* run the quality layer (:mod:`data.quality`) and reject/filter invalid rows
  with an audit trail (never silent);
* write validated long-form OHLCV to ``clean`` Parquet with a per-symbol
  metadata sidecar (source, exchange, ingested_at, quality report);
* register a DuckDB view over the clean Parquet for analytical queries;
* expose a stable dataset fingerprint so experiments can be tied to exactly
  the rows they were computed on.

Nothing here interpolates missing candles and nothing mutates raw files.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import duckdb
import pandas as pd

from config.settings import settings

from .quality import (
    TradingCalendar,
    check_ohlcv_long_frame,
    detect_missing_candles,
    detect_off_calendar_candles,
)

__all__ = ["CleanDataCatalog", "DatasetMetadata", "build_clean_dataset"]


def _source_exchange(source: str, exchange: str) -> tuple[str, str]:
    source = (source or "unknown").strip().lower()
    exchange = (exchange or "NSE").strip().upper()
    return source or "unknown", exchange or "NSE"


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    """Provenance and quality metadata recorded beside each clean file."""

    symbol: str
    source: str
    exchange: str
    rows: int
    validated_at: str
    is_clean: bool
    quality_issues: tuple[str, ...] = ()
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "exchange": self.exchange,
            "rows": self.rows,
            "validated_at": self.validated_at,
            "is_clean": self.is_clean,
            "quality_issues": list(self.quality_issues),
            "fingerprint": self.fingerprint,
        }


class CleanDataCatalog:
    """Build and read the validated clean research dataset."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        duckdb_path: Path | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        data_dir = Path(data_dir) if data_dir is not None else settings.storage.data_dir
        self.raw_dir = data_dir / "raw"
        self.clean_dir = data_dir / "clean"
        self.features_dir = data_dir / "features"
        self.duckdb_path = (
            Path(duckdb_path) if duckdb_path is not None else data_dir / "quant.duckdb"
        )
        self.calendar = calendar

    # -- clean writer --------------------------------------------------------

    def _clean_path(self, source: str, symbol: str) -> Path:
        return self.clean_dir / source / f"{symbol}.parquet"

    def _metadata_path(self, source: str, symbol: str) -> Path:
        return self.clean_dir / source / f"{symbol}.meta.json"

    def write_clean(
        self,
        frame: pd.DataFrame,
        *,
        source: str,
        exchange: str = "NSE",
        symbol: str,
        validated_at: datetime | None = None,
        require_clean: bool = False,
    ) -> tuple[Path, DatasetMetadata]:
        """Validate ``frame`` and write the accepted rows to clean Parquet.

        Raises ``ValueError`` when ``require_clean`` is set and the frame is
        not clean. Returns ``(parquet_path, metadata)``.
        """
        source, exchange = _source_exchange(source, exchange)
        accepted, report = check_ohlcv_long_frame(
            frame, source=source, exchange=exchange
        )
        issues = list(report.issues)
        issues.extend(detect_missing_candles(accepted))
        issues.extend(detect_off_calendar_candles(accepted, calendar=self.calendar))
        if require_clean and issues:
            raise ValueError(
                f"clean dataset rejected for {symbol}: "
                f"{sorted({i.kind for i in issues})}"
            )
        if accepted.empty:
            raise ValueError(f"no valid rows for {symbol}")

        frame_path = self._clean_path(source, symbol)
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        accepted.to_parquet(frame_path, index=False)

        fingerprint = self._frame_fingerprint(accepted)
        validated_at = validated_at or datetime.now(UTC)
        metadata = DatasetMetadata(
            symbol=symbol,
            source=source,
            exchange=exchange,
            rows=len(accepted),
            validated_at=validated_at.isoformat(),
            is_clean=not issues,
            quality_issues=tuple(sorted({i.kind for i in issues})),
            fingerprint=fingerprint,
        )
        self._metadata_path(source, symbol).write_text(
            json.dumps(metadata.to_dict(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return frame_path, metadata

    # -- readers -------------------------------------------------------------

    def read_clean(
        self, symbol: str, source: str = "yfinance"
    ) -> tuple[pd.DataFrame, DatasetMetadata | None]:
        """Return ``(clean_long_frame, metadata)`` for one symbol."""
        source = (source or "unknown").strip().lower()
        path = self._clean_path(source, symbol)
        if not path.is_file():
            raise FileNotFoundError(f"clean data missing for {symbol}: {path}")
        frame = pd.read_parquet(path)
        meta_path = self._metadata_path(source, symbol)
        metadata: DatasetMetadata | None = None
        if meta_path.is_file():
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            metadata = DatasetMetadata(**payload)
        return frame, metadata

    def load_market_panel(
        self,
        symbols: Sequence[str],
        *,
        source: str = "yfinance",
        dropna: bool = True,
    ) -> pd.DataFrame:
        """Pivot clean close prices into a wide date x symbol panel."""
        source = (source or "unknown").strip().lower()
        frames = []
        for symbol in symbols:
            frame, _ = self.read_clean(symbol, source=source)
            frame = frame.copy()
            frame["date"] = pd.to_datetime(frame["date"])
            panel = frame.set_index("date")[["close"]].rename(
                columns={"close": symbol}
            )
            frames.append(panel)
        wide = pd.concat(frames, axis=1).sort_index()
        if dropna:
            wide = wide.dropna(how="all")
        return wide

    @staticmethod
    def _frame_fingerprint(frame: pd.DataFrame) -> str:
        """Return a stable SHA-256 over the sorted long frame."""
        payload = frame.copy()
        payload["date"] = pd.to_datetime(payload["date"]).astype("int64")
        for column in payload.columns:
            if payload[column].dtype == object:
                payload[column] = payload[column].astype(str)
        bytes_ = payload.sort_values(["symbol", "date"]).to_records(
            index=False
        ).tobytes()
        return hashlib.sha256(bytes_).hexdigest()

    def dataset_fingerprint(
        self, symbols: Sequence[str], source: str = "yfinance"
    ) -> str:
        """Fingerprint the concatenation of clean frames for experiment tying."""
        hasher = hashlib.sha256()
        for symbol in sorted(symbols):
            try:
                frame, _ = self.read_clean(symbol, source=source)
            except FileNotFoundError:
                continue
            hasher.update(self._frame_fingerprint(frame).encode("ascii"))
        return hasher.hexdigest()

    # -- DuckDB --------------------------------------------------------------

    def register_duckdb(self, source: str = "yfinance") -> None:
        """Refresh the ``clean_market_data`` DuckDB view over clean Parquet."""
        source = (source or "unknown").strip().lower()
        source_dir = self.clean_dir / source
        self.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.duckdb_path)) as conn:
            if source_dir.is_dir():
                pattern = str(source_dir / "*.parquet")
                conn.execute(
                    f"CREATE OR REPLACE VIEW clean_market_data AS "
                    f"SELECT * FROM read_parquet('{pattern}', filename=true)"
                )

    def query(self, sql: str) -> pd.DataFrame:
        """Run a DuckDB query over the clean dataset."""
        with duckdb.connect(str(self.duckdb_path)) as conn:
            return conn.execute(sql).df()


def build_clean_dataset(
    symbols: Sequence[str],
    frames: Mapping[str, pd.DataFrame],
    *,
    source: str = "yfinance",
    exchange: str = "NSE",
    data_dir: Path | None = None,
    require_clean: bool = True,
    calendar: TradingCalendar | None = None,
) -> dict[str, DatasetMetadata]:
    """Validate and store a set of raw frames into the clean dataset.

    ``frames`` maps symbol -> raw long-form OHLCV frame. Each frame is
    validated, written to clean Parquet with metadata, and the DuckDB view
    is refreshed. Returns a per-symbol metadata mapping.
    """
    catalog = CleanDataCatalog(data_dir, calendar=calendar)
    result: dict[str, DatasetMetadata] = {}
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is None:
            raise ValueError(f"no frame supplied for {symbol}")
        _, metadata = catalog.write_clean(
            frame,
            source=source,
            exchange=exchange,
            symbol=symbol,
            require_clean=require_clean,
        )
        result[symbol] = metadata
    catalog.register_duckdb(source=source)
    return result
