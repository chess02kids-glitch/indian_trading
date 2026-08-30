import yfinance as yf
import pandas as pd
from pathlib import Path

def main():
    print("Fetching Market Indices (Nifty 50, India VIX)...")
    indices = {
        "^NSEI": "NIFTY_50",
        "^INDIAVIX": "INDIA_VIX"
    }
    
    out_dir = Path("data/market/indices")
    out_dir.mkdir(parents=True, exist_ok=True)
    
    for ticker, name in indices.items():
        print(f"Fetching {name} ({ticker})...")
        try:
            # Fetch max history available
            df = yf.download(ticker, progress=False, period="max")
            if not df.empty:
                # Flatten multi-index columns if present
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.droplevel(1)
                
                df.index = pd.to_datetime(df.index)
                df.index = df.index.tz_localize(None)
                df.reset_index(inplace=True)
                df.rename(columns={"Date": "date", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}, inplace=True)
                df = df[["date", "open", "high", "low", "close", "volume"]]
                
                out_path = out_dir / f"{name}.parquet"
                df.to_parquet(out_path, index=False)
                print(f"  -> Saved {len(df)} daily rows to {out_path}")
            else:
                print(f"  -> No data found for {name}.")
        except Exception as e:
            print(f"Error fetching {name}: {e}")

if __name__ == "__main__":
    main()
