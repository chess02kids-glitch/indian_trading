import os
import glob
import pandas as pd
from datetime import datetime

def reshard_clean_to_raw():
    clean_dir = 'data/clean/eod2_data'
    raw_dir = 'data/raw/eod2_data/NSE'
    
    clean_files = glob.glob(os.path.join(clean_dir, '*.parquet'))
    print(f"Found {len(clean_files)} files in clean layer.")
    
    for file in clean_files:
        symbol = os.path.basename(file).replace('.parquet', '')
        print(f"Processing {symbol}...")
        df = pd.read_parquet(file)
        
        # Make sure date is datetime
        if not pd.api.types.is_datetime64_any_dtype(df['date']):
            df['date'] = pd.to_datetime(df['date'])
            
        df['year'] = df['date'].dt.year
        df['month'] = df['date'].dt.month
        
        for (year, month), group in df.groupby(['year', 'month']):
            path = os.path.join(raw_dir, symbol, str(year))
            os.makedirs(path, exist_ok=True)
            # month:02d ensures zero padding like 01, 02
            file_path = os.path.join(path, f"{month:02d}.parquet")
            
            # The schema expected: date, symbol, open, high, low, close, volume, series, source, exchange, ingested_at, source_ts, adjustment_state
            # Assuming clean layer has these, or we drop year/month
            out_df = group.drop(columns=['year', 'month'])
            out_df.to_parquet(file_path, index=False)

if __name__ == "__main__":
    reshard_clean_to_raw()
    print("Resharding complete.")
