import pandas as pd
import pandera as pa
from pandera import Check, Column, DataFrameSchema

from config.settings import settings
from observability.logging import ContextLogger, get_logger

base_logger = get_logger("quant_india.ingestion.validation")


def check_high_low(df: pd.DataFrame) -> pd.Series:
    return df["High"] >= df["Low"]


def check_open_in_range(df: pd.DataFrame) -> pd.Series:
    return (df["Open"] <= df["High"]) & (df["Open"] >= df["Low"])


def check_close_in_range(df: pd.DataFrame) -> pd.Series:
    return (df["Close"] <= df["High"]) & (df["Close"] >= df["Low"])


def check_outliers(df: pd.DataFrame) -> pd.Series:
    """Volatility aware outlier detection on returns."""
    if len(df) < 2:
        return pd.Series([True] * len(df), index=df.index)

    returns = df["Close"].pct_change().dropna()
    mean = returns.mean()
    std = returns.std()

    if pd.isna(std) or std == 0:
        return pd.Series([True] * len(df), index=df.index)

    threshold = settings.validation.volatility_threshold
    # Valid if absolute return is within mean +/- threshold * std
    valid_returns = (returns >= mean - threshold * std) & (
        returns <= mean + threshold * std
    )

    # First row is always true since return is NaN
    result = pd.Series([True] * len(df), index=df.index)
    result.loc[valid_returns.index] = valid_returns
    return result


ohlcv_schema = DataFrameSchema(
    {
        "date": Column(
            pd.DatetimeTZDtype(tz="UTC") if False else "datetime64[ns]",
            coerce=True,
            required=True,
        ),
        "Open": Column(float, checks=[Check(lambda x: x > 0)]),
        "High": Column(float, checks=[Check(lambda x: x > 0)]),
        "Low": Column(float, checks=[Check(lambda x: x > 0)]),
        "Close": Column(float, checks=[Check(lambda x: x > 0)]),
        "Volume": Column(float, coerce=True, checks=[Check(lambda x: x >= 0)]),
    },
    checks=[
        Check(check_high_low, ignore_na=False, error="High < Low"),
        Check(check_open_in_range, ignore_na=False, error="Open outside High/Low"),
        Check(check_close_in_range, ignore_na=False, error="Close outside High/Low"),
        Check(check_outliers, ignore_na=False, error="Outliers detected"),
    ],
)


class ValidationEngine:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.logger = ContextLogger(base_logger, symbol=symbol)

    def validate_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """Runs the validation schema on the dataframe."""
        if df.empty:
            self.logger.warning("Empty dataframe, nothing to validate.")
            return df

        # Detect duplicates
        if df.duplicated(subset=["date"]).any():
            self.logger.error("Duplicate records detected.")
            raise ValueError(f"Duplicate records found for {self.symbol}.")

        # Stale data check
        last_date = df["date"].max()
        if pd.isna(last_date):
            self.logger.error("No valid dates found.")
            raise ValueError("Stale data check failed: No valid date.")

        # Determine if stale (e.g., last date is more than 5 days ago)
        # Using tz-naive comparison if needed
        now = pd.Timestamp.now(tz=last_date.tz) if last_date.tz else pd.Timestamp.now()
        days_diff = (now - last_date).days
        if days_diff > 5:
            self.logger.warning(
                f"Data might be stale. Last date is {last_date}, {days_diff} days ago."
            )

        try:
            validated_df = ohlcv_schema.validate(df, lazy=True)
            self.logger.info("Validation passed successfully.")
            return validated_df
        except pa.errors.SchemaErrors as err:
            self.logger.error(f"Validation failed: {err.failure_cases}")
            raise ValueError(
                f"Data validation failed for {self.symbol}: {err.failure_cases}"
            )
