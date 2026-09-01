import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StorageConfig:
    """Storage configuration for paths and databases.

    AUDIT-027: ``data_dir`` is a dataclass field whose default is evaluated
    **once**, when ``settings`` is constructed — which happens at import.
    Because ``settings`` is a module-level singleton, a later
    ``QUANT_DATA_DIR`` change (a test fixture monkeypatching the
    environment, or a supervisor rewriting it) has no effect by itself, and
    every caller keeps writing into the committed ``data/``. Two things fix
    that:

    * ``StorageManager`` / ``DuckDBManager`` no longer bind
      ``settings.storage.*`` as *default arguments* (which Python evaluates
      at import), so they read the current value at construction time;
    * :meth:`rebind` re-resolves the field from the current environment for
      callers that need it.

    Tests redirect the attribute directly with
    ``monkeypatch.setattr(settings.storage, "data_dir", path)``, which is
    restored correctly — a property-plus-override scheme was tried first and
    leaked a stale override into later tests.
    """

    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("QUANT_DATA_DIR", "data"))
    )

    def rebind(self, data_dir: Path | str | None = None) -> Path:
        """Re-resolve ``data_dir`` from the environment and return it.

        AUDIT-027: the default is evaluated once, when ``settings`` is
        constructed, so a later ``QUANT_DATA_DIR`` change (a test fixture
        monkeypatching the environment, or a supervisor rewriting it) had no
        effect and every caller kept writing into the committed ``data/``.
        Callers that must honour the current environment call this first.
        """
        self.data_dir = Path(data_dir) if data_dir is not None else Path(
            os.getenv("QUANT_DATA_DIR", "data")
        )
        return self.data_dir

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
