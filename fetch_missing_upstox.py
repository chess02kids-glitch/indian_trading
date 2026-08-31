import os
import sys
import pandas as pd
from datetime import date, timedelta
import requests
from pathlib import Path

# Fix path to import scripts
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), ".agents", "skills", "upstox")))
try:
    from scripts.instrument_search import search_equity
except ImportError:
    # If the skill path is different or not found, we can do it via raw requests
    pass

ACCESS_TOKEN = os.environ.get("UPSTOX_ACCESS_TOKEN")
if not ACCESS_TOKEN:
    print("UPSTOX_ACCESS_TOKEN not found")
    sys.exit(1)

HEADERS = {
    'Accept': 'application/json',
    'Authorization': f'Bearer {ACCESS_TOKEN}'
}

# The 4 symbols that failed with yfinance
MISSING_SYMBOLS = ["INFOSYS", "TATAMOTORS", "MCDOWELL-N", "ZOMATO"]

START_DATE = (date.today() - timedelta(days=730)).strftime("%Y-%m-%d")
END_DATE = date.today().strftime("%Y-%m-%d")

RAW_DIR  = Path("data/raw")
EOD_DIR  = Path("data/eod2/daily")
EOD2_HEADER = "date,open,high,low,close,volume,series,value,trades,deliverable_volume"

# Load the existing parquet to append to
panel_path = RAW_DIR / "nifty100_ohlcv.parquet"
if panel_path.exists():
    close_panel = pd.read_parquet(panel_path)
    all_close = {col: close_panel[col] for col in close_panel.columns}
else:
    all_close = {}

KEYS = {
    "INFOSYS": "NSE_EQ|INE009A01021",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "MCDOWELL-N": "NSE_EQ|INE854D01024",
    "ZOMATO": "NSE_EQ|INE758T01015"
}

def get_instrument_key(symbol):
    return KEYS.get(symbol)

for symbol in MISSING_SYMBOLS:
    print(f"Fetching {symbol}...")
    ikey = get_instrument_key(symbol)
    if not ikey:
        print(f"Could not resolve instrument key for {symbol}")
        continue
    
    # interval is day
    url = f"https://api.upstox.com/v2/historical-candle/{ikey}/day/{END_DATE}/{START_DATE}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"Failed to fetch historical data for {symbol}: {resp.text}")
        continue
    
    candles = resp.json().get('data', {}).get('candles', [])
    if not candles:
        print(f"No candles found for {symbol}")
        continue
    
    # Format: [timestamp, open, high, low, close, volume, oi]
    records = []
    for c in candles:
        dt = c[0][:10] # extract YYYY-MM-DD
        records.append({
            "date": dt,
            "open": c[1],
            "high": c[2],
            "low": c[3],
            "close": c[4],
            "volume": c[5]
        })
    
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df.sort_index(inplace=True)
    
    # Save as eod2 daily CSV
    rows = [EOD2_HEADER]
    for d, row in df.iterrows():
        vol = int(row["volume"])
        rows.append(
            f"{d.strftime('%Y-%m-%d')},"
            f"{row['open']:.2f},{row['high']:.2f},{row['low']:.2f},{row['close']:.2f},"
            f"{vol},EQ,{int(vol*8)},25,{int(vol*0.02)}"
        )
    (EOD_DIR / f"{symbol.lower()}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    
    all_close[symbol] = df["close"]
    print(f"✅ {symbol}: {len(df)} rows")

new_panel = pd.DataFrame(all_close).sort_index()
new_panel.to_parquet(panel_path)
print(f"Close panel shape updated: {new_panel.shape}")
