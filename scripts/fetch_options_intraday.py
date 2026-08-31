import os
import json
from pathlib import Path

import pandas as pd
import upstox_client
from upstox_client.rest import ApiException

def main():
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        print("UPSTOX_ACCESS_TOKEN not set in environment. Generating mock options and intraday data...")
        generate_mock_data()
        return

    configuration = upstox_client.Configuration()
    configuration.access_token = token
    client = upstox_client.ApiClient(configuration)

    options_api = upstox_client.OptionsApi(client)
    history_api = upstox_client.HistoryV3Api(client)
    
    out_dir = Path("data/market/options")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    intraday_dir = Path("data/market/intraday")
    intraday_dir.mkdir(parents=True, exist_ok=True)

    print("Attempting to fetch real Options data for Nifty 50...")
    try:
        contracts = options_api.get_option_contracts(instrument_key="NSE_INDEX|Nifty 50")
        expiries = sorted({c.expiry for c in contracts.data})
        if expiries:
            target_expiry = expiries[0]
            print(f"Fetching chain for {target_expiry}...")
            chain = options_api.get_put_call_option_chain(
                instrument_key="NSE_INDEX|Nifty 50",
                expiry_date=target_expiry
            ).data
            
            records = []
            for s in chain:
                ce, pe = s.call_options, s.put_options
                records.append({
                    "strike": s.strike_price,
                    "ce_ltp": ce.market_data.ltp,
                    "ce_iv": ce.option_greeks.iv,
                    "ce_oi": ce.market_data.oi,
                    "pe_ltp": pe.market_data.ltp,
                    "pe_iv": pe.option_greeks.iv,
                    "pe_oi": pe.market_data.oi,
                })
            df = pd.DataFrame(records)
            df.to_parquet(out_dir / "nifty50_options.parquet")
            print(f"Saved {len(df)} option chain rows.")
    except ApiException as e:
        print("Upstox Options fetch failed:", getattr(e, "body", e))
        generate_mock_data()
        return

    print("Attempting to fetch 1-minute intraday data...")
    try:
        # Get historical 1-minute candle data for Nifty 50
        # interval="1minute", to_date="2026-08-31", from_date="2026-08-30" (or similar short range)
        candles = history_api.get_historical_candle_data(
            instrument_key="NSE_INDEX|Nifty 50",
            interval="1minute",
            to_date="2026-08-31"
        )
        if candles.data and candles.data.candles:
            records = []
            for c in candles.data.candles:
                # c is [timestamp, open, high, low, close, volume, oi]
                records.append({
                    "date": c[0],
                    "open": c[1],
                    "high": c[2],
                    "low": c[3],
                    "close": c[4],
                    "volume": c[5]
                })
            df_intra = pd.DataFrame(records)
            df_intra.to_parquet(intraday_dir / "nifty50_intraday.parquet")
            print(f"Saved {len(df_intra)} intraday rows.")
    except ApiException as e:
        print("Upstox Intraday fetch failed:", getattr(e, "body", e))
        generate_mock_data()


def generate_mock_data():
    print("Generating mock Intraday and Options data as fallback...")
    
    out_dir = Path("data/market/options")
    out_dir.mkdir(parents=True, exist_ok=True)
    df_opt = pd.DataFrame({
        "strike": [25000, 25100, 25200, 25300],
        "ce_ltp": [120, 80, 50, 30],
        "ce_iv": [12.5, 12.8, 13.0, 13.5],
        "ce_oi": [10000, 20000, 15000, 5000],
        "pe_ltp": [30, 50, 80, 120],
        "pe_iv": [13.5, 13.0, 12.8, 12.5],
        "pe_oi": [5000, 15000, 20000, 10000],
    })
    df_opt.to_parquet(out_dir / "nifty50_options.parquet")
    
    intraday_dir = Path("data/market/intraday")
    intraday_dir.mkdir(parents=True, exist_ok=True)
    dates = pd.date_range("2026-08-31 09:15:00", "2026-08-31 15:30:00", freq="1min")
    df_intra = pd.DataFrame({
        "date": dates,
        "open": 25000,
        "high": 25010,
        "low": 24990,
        "close": 25005,
        "volume": 1000
    })
    df_intra.to_parquet(intraday_dir / "nifty50_intraday.parquet")
    print("Mock data generated.")


if __name__ == "__main__":
    main()
