from typing import List

from config.settings import settings
from data.duckdb_manager import DuckDBManager
from data.storage import StorageManager
from ingestion.validation import ValidationEngine
from ingestion.yfinance_client import YFinanceClient
from observability.logging import ContextLogger, get_logger

base_logger = get_logger("quant_india.ingestion.pipeline")


class IngestionPipeline:
    """Orchestrates fetching, validation, and storage of data."""

    def __init__(self):
        self.logger = ContextLogger(base_logger, operation="pipeline")
        self.client = YFinanceClient()
        self.storage = StorageManager()
        self.db = DuckDBManager()

    def ingest_symbol(self, symbol: str, period: str = "max") -> None:
        """Pipeline for a single symbol."""
        logger = ContextLogger(base_logger, symbol=symbol, operation="ingest_symbol")
        logger.info(f"Starting ingestion pipeline for {symbol}")

        try:
            # 1. Fetch
            df = self.client.fetch_symbol(symbol, period)
            if df.empty:
                logger.warning(f"No data returned for {symbol}")
                return

            # 2. Validate
            validator = ValidationEngine(symbol)
            df_valid = validator.validate_df(df)

            # 3. Store
            self.storage.save_historical_data(
                df=df_valid,
                source=settings.ingestion.default_source,
                exchange=settings.ingestion.default_exchange,
                symbol=symbol,
            )
            logger.info(f"Successfully ingested {symbol}")

        except Exception as e:
            logger.error(f"Pipeline failed for {symbol}: {e}")
            raise

    def ingest_universe(self, symbols: List[str], period: str = "max") -> None:
        """Pipeline for a universe of symbols."""
        self.logger.info(
            f"Starting ingestion pipeline for universe of {len(symbols)} symbols"
        )

        success_count = 0
        for symbol in symbols:
            try:
                self.ingest_symbol(symbol, period)
                success_count += 1
            except Exception as e:
                self.logger.error(f"Failed to ingest {symbol} in universe: {e}")

        self.logger.info(
            f"Universe ingestion complete. {success_count}/{len(symbols)} successful."
        )

        # Re-initialize DuckDB views since new data might be present
        self.db._init_db()
