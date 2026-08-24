import time
from typing import Dict, List

import pandas as pd
import yfinance as yf

from config.settings import settings
from observability.logging import ContextLogger, get_logger

base_logger = get_logger("quant_india.ingestion.yfinance")


class YFinanceClient:
    """Client for fetching data from Yahoo Finance."""

    def __init__(self):
        self.logger = ContextLogger(base_logger, operation="yfinance")

    def _normalize_df(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Normalizes the dataframe returned by yfinance."""
        if df.empty:
            return df

        df = df.reset_index()
        # yfinance returns index as Date or Datetime
        col_rename = {}
        for col in df.columns:
            if str(col).lower() in ["index", "date", "datetime"]:
                col_rename[col] = "date"
        df = df.rename(columns=col_rename)

        # Ensure standard OHLCV columns exist
        required_cols = ["date", "Open", "High", "Low", "Close", "Volume"]
        for col in required_cols:
            if col not in df.columns:
                self.logger.warning(
                    f"Missing column '{col}', adding empty.", context={"symbol": symbol}
                )
                if col == "date":
                    raise ValueError("Date column missing.")
                df[col] = 0.0 if col != "Volume" else 0

        # convert dates to naive UTC
        df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
        return df[required_cols]

    def fetch_symbol(self, symbol: str, period: str = "max") -> pd.DataFrame:
        """Fetches historical OHLCV data for a single symbol with retries."""
        logger = ContextLogger(base_logger, symbol=symbol, operation="fetch_symbol")

        for attempt in range(1, settings.ingestion.max_retries + 1):
            try:
                logger.info(f"Fetching data for {symbol} (attempt {attempt})")
                ticker = yf.Ticker(symbol)
                df = ticker.history(period=period)
                df = self._normalize_df(df, symbol)
                logger.info(f"Successfully fetched {len(df)} rows for {symbol}")
                return df
            except Exception as e:
                logger.warning(f"Attempt {attempt} failed: {e}")
                if attempt == settings.ingestion.max_retries:
                    logger.error(
                        f"Failed to fetch {symbol} after {settings.ingestion.max_retries} attempts."
                    )
                    raise
                time.sleep(settings.ingestion.retry_backoff * attempt)

        return pd.DataFrame()

    def fetch_batch(
        self, symbols: List[str], period: str = "max"
    ) -> Dict[str, pd.DataFrame]:
        """Fetches historical OHLCV data for a batch of symbols."""
        self.logger.info(f"Fetching batch of {len(symbols)} symbols")
        results = {}
        for symbol in symbols:
            try:
                results[symbol] = self.fetch_symbol(symbol, period)
            except Exception as e:
                self.logger.error(f"Batch fetch failed for {symbol}: {e}")
                results[symbol] = pd.DataFrame()
        return results
