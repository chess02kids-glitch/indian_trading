# Ingestion Guide

The ingestion pipeline (`ingestion/pipeline.py`) manages the orchestration of fetching data from the external source, validating it, and saving it to storage.

## Usage
via CLI:
```bash
python main.py ingest --symbol RELIANCE.NS
python main.py ingest --universe RELIANCE.NS,TCS.NS
```

## Providers
Currently, the primary data source is `yfinance`.
The `YFinanceClient` implements robust retries and normalizes returned pandas dataframes so they match the standard OHLCV requirements.
