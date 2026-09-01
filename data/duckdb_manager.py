from pathlib import Path

import duckdb
import pandas as pd

from config.settings import settings
from observability.logging import ContextLogger, get_logger

base_logger = get_logger("quant_india.data.duckdb")


class DuckDBManager:
    """Manages DuckDB analytical layer over Parquet storage."""

    def __init__(
        self,
        db_path: Path | None = None,
        data_dir: Path | None = None,
    ):
        # AUDIT-027: the defaults used to be evaluated at *import* time
        # (``db_path: Path = settings.storage.duckdb_path``), so
        # ``QUANT_DATA_DIR`` was frozen before any fixture could redirect
        # it and a test run wrote straight into the committed
        # ``data/quant.duckdb``. Resolve lazily instead.
        self.db_path = (
            Path(db_path) if db_path is not None else settings.storage.duckdb_path
        )
        self.data_dir = (
            Path(data_dir) if data_dir is not None else settings.storage.raw_dir
        )
        self.logger = ContextLogger(base_logger, operation="duckdb")
        self._init_db()

    def _init_db(self) -> None:
        """Initialize DuckDB database and register standard views."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with duckdb.connect(str(self.db_path)) as conn:
                parquet_glob = str(self.data_dir / "*" / "*" / "*" / "*" / "*.parquet")
                files_exist = any(self.data_dir.rglob("*.parquet"))

                if files_exist:
                    # Create a view that unions all parquet files in the raw directory
                    query = f"CREATE OR REPLACE VIEW market_data AS SELECT * FROM read_parquet('{parquet_glob}', filename=true);"
                    conn.execute(query)
                    self.logger.info(
                        "Initialized market_data view over raw parquet files."
                    )
                else:
                    self.logger.warning(
                        "No parquet files found yet, skipped view creation."
                    )
        except Exception as e:
            self.logger.error(f"Failed to initialize DuckDB: {e}")
            raise

    def execute(self, query: str) -> pd.DataFrame:
        """Execute a query and return results as a Pandas DataFrame."""
        try:
            with duckdb.connect(str(self.db_path)) as conn:
                return conn.execute(query).df()
        except Exception as e:
            self.logger.error(f"Query execution failed: {e}")
            raise

    def create_snapshot(self, name: str) -> Path:
        """Exports the current market_data view to a single snapshot parquet file."""
        # AUDIT-027: ``settings.storage.data_dir`` is a property, so this is
        # resolved at call time rather than being bound at import time.
        snapshot_dir = settings.storage.data_dir / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"{name}.parquet"

        try:
            with duckdb.connect(str(self.db_path)) as conn:
                # check if view exists
                views = conn.execute(
                    "SELECT view_name FROM duckdb_views() WHERE view_name='market_data'"
                ).fetchall()
                if not views:
                    self.logger.error(
                        "market_data view does not exist. Cannot create snapshot."
                    )
                    return snapshot_path

                query = f"COPY (SELECT * FROM market_data) TO '{snapshot_path}' (FORMAT PARQUET);"
                conn.execute(query)
                self.logger.info(f"Created snapshot at {snapshot_path}")
                return snapshot_path
        except Exception as e:
            self.logger.error(f"Failed to create snapshot: {e}")
            raise
