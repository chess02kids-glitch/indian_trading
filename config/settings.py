import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StorageConfig:
    """Storage configuration for paths and databases.

    AUDIT-027: ``data_dir`` used to be a dataclass field whose default was
    evaluated **once, when the module was imported**. Because ``settings``
    is a module-level singleton, ``QUANT_DATA_DIR`` was frozen before any
    test fixture (or a ``os.environ`` change at runtime) could redirect it,
    so a test run wrote straight into the committed ``data/quant.duckdb``
    and ``data/snapshots/`` and left the working tree dirty. It is a
    property now, resolved on every access.
    """

    _data_dir_override: Path | None = field(default=None, repr=False)

    @property
    def data_dir(self) -> Path:
        # An explicit assignment wins over the environment, so deployments
        # and tests can still pin a directory.
        return self._data_dir_override or Path(os.getenv("QUANT_DATA_DIR", "data"))

    @data_dir.setter
    def data_dir(self, value: Path | str | None) -> None:
        self._data_dir_override = Path(value) if value is not None else None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def clean_dir(self) -> Path:
        return self.data_dir / "clean"

    @property
    def duckdb_path(self) -> Path:
        return self.data_dir / "quant.duckdb"


@dataclass
class IngestionConfig:
    """Configuration for data ingestion."""

    default_source: str = "yfinance"
    default_exchange: str = "NSE"
    max_retries: int = int(os.getenv("QUANT_MAX_RETRIES", "3"))
    retry_backoff: float = float(os.getenv("QUANT_RETRY_BACKOFF", "2.0"))


@dataclass
class ValidationConfig:
    """Configuration for data validation thresholds."""

    volatility_threshold: float = float(os.getenv("QUANT_VOLATILITY_THRESHOLD", "3.0"))


@dataclass
class Settings:
    """Central configuration for Quant India Data Platform."""

    storage: StorageConfig = field(default_factory=StorageConfig)
    ingestion: IngestionConfig = field(default_factory=IngestionConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)


settings = Settings()
