import json
import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import datetime

def main():
    print("Fetching Sector, Industry, and Market Cap...")
    with open('data/universe/nifty100_symbols.txt', 'r') as f:
        symbols = [s.strip() for s in f.readlines() if s.strip()]

    records = []
    
    for i, symbol in enumerate(symbols):
        if symbol == "symbol": continue
        ticker = f"{symbol}.NS"
        try:
            info = yf.Ticker(ticker).info
            records.append({
                "symbol": symbol,
                "sector": info.get("sector", "Unknown"),
                "industry": info.get("industry", "Unknown"),
                "market_cap": info.get("marketCap", None),
                "free_float": info.get("floatShares", None),
                "shares_outstanding": info.get("sharesOutstanding", None),
                "avg_daily_traded_value": info.get("averageVolume10days", 0) * info.get("currentPrice", 0) if info.get("averageVolume10days") and info.get("currentPrice") else None,
                "date": datetime.today().strftime('%Y-%m-%d')
            })
            if i % 10 == 0:
                print(f"  fetched {i}/{len(symbols)}...")
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")

    df = pd.DataFrame(records)
    
    # Split into sector map and market cap
    sector_df = df[["symbol", "sector", "industry"]]
    cap_df = df[["date", "symbol", "market_cap", "free_float", "shares_outstanding", "avg_daily_traded_value"]]
    
    out_dir_universe = Path("data/universe")
    out_dir_market = Path("data/market")
    out_dir_universe.mkdir(parents=True, exist_ok=True)
    out_dir_market.mkdir(parents=True, exist_ok=True)
    
    sector_df.to_csv(out_dir_universe / "sector_map.csv", index=False)
    cap_df.to_csv(out_dir_market / "float.csv", index=False)
    
    print(f"Successfully wrote {len(sector_df)} records to sector_map.csv and float.csv")

if __name__ == "__main__":
    main()
