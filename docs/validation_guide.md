# Validation Guide

All ingested market data passes through `pandera` schema validations before being stored.

## Checks Implemented
- **Schema typing:** Date is datetime, OHLC are floats, Volume is float.
- **Positive Values:** All prices must be > 0.
- **High/Low Integrity:** `High >= Low`.
- **Open/Close bounds:** Open and Close must be within `High` and `Low`.
- **Duplicate Records:** No duplicate dates for the same symbol.
- **Stale Data:** Warnings are emitted if the last fetched record is > 5 days old.
- **Volatility-aware Outliers:** Reject rows where absolute return is outside of `mean +/- (volatility_threshold * std)`.

Failed validations raise exceptions and halt the pipeline for the symbol, emitting a structured JSON log without corrupting the historical database.
