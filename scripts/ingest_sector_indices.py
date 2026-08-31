import re
from pathlib import Path

import pandas as pd


def main():
    print("Ingesting Sector and Broad Indices from eod2...")

    src_dir = Path("data/eod2/daily")
    if not src_dir.exists():
        print(f"Error: {src_dir} does not exist.")
        return

    out_indices = Path("data/market/indices")
    out_sectors = Path("data/market/sector_indices")
    
    out_indices.mkdir(parents=True, exist_ok=True)
    out_sectors.mkdir(parents=True, exist_ok=True)

    header_map = {
        "date": "Date", "open": "Open", "high": "High", "low": "Low", 
        "close": "Close", "volume": "Volume"
    }

    count = 0
    
    for csv_file in src_dir.glob("nifty*.csv"):
        name = csv_file.stem.upper().replace(" ", "_")
        
        try:
            df = pd.read_csv(csv_file)
            # rename to canonical
            df.rename(columns=lambda x: header_map.get(str(x).strip().lower(), str(x)), inplace=True)
            
            # format as expected by market/indices
            df["date"] = pd.to_datetime(df.get("Date", df.get("date")))
            df["open"] = pd.to_numeric(df.get("Open", df.get("open")), errors="coerce")
            df["high"] = pd.to_numeric(df.get("High", df.get("high")), errors="coerce")
            df["low"] = pd.to_numeric(df.get("Low", df.get("low")), errors="coerce")
            df["close"] = pd.to_numeric(df.get("Close", df.get("close")), errors="coerce")
            df["volume"] = pd.to_numeric(df.get("Volume", df.get("volume")), errors="coerce").fillna(0)
            
            df = df[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["date", "close"])
            
            # Sort
            df.sort_values("date", inplace=True)
            
            if len(df) == 0:
                continue

            # Route to sector or broad
            is_broad = bool(re.match(r"^NIFTY_?(50|100|200|500|NEXT_50|MIDCAP|SMALLCAP|LARGEMIDCAP|MICROCAP|TOTAL_MARKET)", name))
            
            out_path = (out_indices if is_broad else out_sectors) / f"{name}.parquet"
            df.to_parquet(out_path, index=False)
            count += 1
            
        except Exception as e:
            print(f"Error processing {csv_file.name}: {e}")

    print(f"Ingested {count} indices successfully.")


if __name__ == "__main__":
    main()
