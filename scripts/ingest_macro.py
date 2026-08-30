import os
import requests
import pandas as pd
from datetime import datetime
from pathlib import Path

# The user's FRED API key
FRED_API_KEY = "0a7fba5965eb42e16d16f0eee41a9bb8"

# FRED Series IDs for India and global macro
SERIES = {
    "cpi": "INDCPIALLMINMEI",           # Consumer Price Index: All Items for India
    "inr_usd": "DEXINUS",               # India / U.S. Foreign Exchange Rate (Daily)
    "crude_price": "DCOILWTICO",        # Crude Oil Prices: West Texas Intermediate (WTI)
    "10y_gilt_yield": "INDIRLTLT01STM", # Long-Term Interest Rates for India (Monthly)
    "repo_rate": "INTDSRINM193N",       # Interest Rates, Discount Rate for India
}

def fetch_series(series_id):
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={FRED_API_KEY}&file_type=json"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        observations = data.get("observations", [])
        df = pd.DataFrame(observations)[["date", "value"]]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df.rename(columns={"value": series_id}, inplace=True)
        df.set_index("date", inplace=True)
        return df
    else:
        print(f"Failed to fetch {series_id}: {response.text}")
        return pd.DataFrame()

def main():
    print("Fetching macro data from FRED...")
    dfs = []
    for name, series_id in SERIES.items():
        print(f"  Fetching {name} ({series_id})...")
        df = fetch_series(series_id)
        if not df.empty:
            df.rename(columns={series_id: name}, inplace=True)
            dfs.append(df)
            
    if dfs:
        # Merge all dataframes on date
        merged = dfs[0]
        for df in dfs[1:]:
            merged = merged.join(df, how="outer")
            
        # Forward fill the daily gaps for monthly/quarterly series
        merged.index = pd.to_datetime(merged.index)
        merged.sort_index(inplace=True)
        merged = merged.ffill()
        
        # Filter from 2010 onwards
        merged = merged.loc["2010-01-01":]
        
        merged.reset_index(inplace=True)
        
        out_dir = Path("data/market")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "macro.csv"
        merged.to_csv(out_path, index=False)
        print(f"Successfully wrote {len(merged)} macro rows to {out_path}")
    else:
        print("Failed to fetch macro data.")

if __name__ == "__main__":
    main()
