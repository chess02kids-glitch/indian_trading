import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StorageConfig:
    """Storage configuration for paths and databases."""

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("QUANT_DATA_DIR", "data"))
    )

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
