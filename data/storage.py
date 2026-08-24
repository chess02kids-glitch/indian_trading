from pathlib import Path

import pandas as pd

from config.settings import settings
from observability.logging import ContextLogger, get_logger

base_logger = get_logger("quant_india.data.storage")


class StorageManager:
    """Manages raw historical parquet storage with monthly partitioning."""

    def __init__(self, data_dir: Path = settings.storage.raw_dir):
        self.data_dir = data_dir
        self.logger = ContextLogger(base_logger, operation="storage")

    def _get_partition_path(
        self, source: str, exchange: str, symbol: str, year: int, month: int
    ) -> Path:
        """Get the specific file path for a monthly partition."""
        # e.g., data/raw/yfinance/NSE/RELIANCE/2023/10.parquet
        return (
            self.data_dir
            / source
            / exchange
            / symbol
            / str(year)
            / f"{month:02d}.parquet"
        )

    def save_historical_data(
        self, df: pd.DataFrame, source: str, exchange: str, symbol: str
    ) -> None:
        """
        Saves historically downloaded data to parquet partitions.
        Preserves existing data and appends/overwrites logically.
        """
        if df.empty:
            self.logger.warning(
                "Empty dataframe received, skipping storage", context={"symbol": symbol}
            )
            return

        # Ensure we have a datetime column 'date' for grouping
        if "date" not in df.columns:
            if isinstance(df.index, pd.DatetimeIndex):
                df = df.reset_index()
                # Rename the index column to 'date'
                col_rename = {}
                for col in df.columns:
                    if str(col).lower() in ["index", "date", "datetime"]:
                        col_rename[col] = "date"
                df = df.rename(columns=col_rename)
            else:
                raise ValueError(
                    "DataFrame must contain a 'date' column or a DatetimeIndex."
                )

        # Ensure date is datetime
        df["date"] = pd.to_datetime(df["date"])

        # Add metadata columns
        df["source"] = source
        df["exchange"] = exchange
        df["symbol"] = symbol

        # Sort by date
        df = df.sort_values(by="date")

        # Group by Year and Month to save in partitions
        for (year, month), group in df.groupby(
            [df["date"].dt.year, df["date"].dt.month]
        ):
            path = self._get_partition_path(source, exchange, symbol, year, month)
            path.parent.mkdir(parents=True, exist_ok=True)

            if path.exists():
                try:
                    existing_df = pd.read_parquet(path)
                    combined = pd.concat([existing_df, group])
                    # Deduplicate on date, keeping the last (newest) record
                    combined = combined.drop_duplicates(subset=["date"], keep="last")
                    combined = combined.sort_values("date")
                    combined.to_parquet(path, index=False)
                    self.logger.info(
                        f"Updated partition: {year}/{month:02d}",
                        context={"symbol": symbol, "path": str(path)},
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to update partition: {e}",
                        context={"symbol": symbol, "path": str(path)},
                    )
                    raise
            else:
                try:
                    group.to_parquet(path, index=False)
                    self.logger.info(
                        f"Created partition: {year}/{month:02d}",
                        context={"symbol": symbol, "path": str(path)},
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to write partition: {e}",
                        context={"symbol": symbol, "path": str(path)},
                    )
                    raise
