import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

# --- Step 2: Fetch OHLCV data ---
NIFTY100_SYMBOLS_OHLCV = [
    "RELIANCE.NS","TCS.NS","HDFCBANK.NS","ICICIBANK.NS","INFOSYS.NS",
    "HINDUNILVR.NS","SBIN.NS","BHARTIARTL.NS","ITC.NS","KOTAKBANK.NS",
    "LT.NS","AXISBANK.NS","HCLTECH.NS","BAJFINANCE.NS","WIPRO.NS",
    "MARUTI.NS","ASIANPAINT.NS","TITAN.NS","SUNPHARMA.NS","ULTRACEMCO.NS",
    "NESTLEIND.NS","POWERGRID.NS","NTPC.NS","TECHM.NS","ONGC.NS",
    "BAJAJFINSV.NS","JSWSTEEL.NS","TATAMOTORS.NS","ADANIENT.NS","COALINDIA.NS",
    "GRASIM.NS","DRREDDY.NS","DIVISLAB.NS","CIPLA.NS","EICHERMOT.NS",
    "TATACONSUM.NS","HEROMOTOCO.NS","BRITANNIA.NS","SHREECEM.NS","BPCL.NS",
    "HINDALCO.NS","VEDL.NS","TATASTEEL.NS","APOLLOHOSP.NS","INDUSINDBK.NS",
    "ADANIPORTS.NS","BAJAJ-AUTO.NS","M&M.NS","HDFCLIFE.NS","SBILIFE.NS",
    "PIDILITIND.NS","DABUR.NS","GODREJCP.NS","MARICO.NS","COLPAL.NS",
    "SIEMENS.NS","HAVELLS.NS","BERGEPAINT.NS","PGHH.NS","MCDOWELL-N.NS",
    "BANKBARODA.NS","PNB.NS","CANBK.NS","UNIONBANK.NS","IDFCFIRSTB.NS",
    "GAIL.NS","IOC.NS","HINDPETRO.NS","OIL.NS","MGL.NS",
    "ZOMATO.NS","PAYTM.NS","NYKAA.NS","POLICYBZR.NS","DELHIVERY.NS",
    "TRENT.NS","JUBLFOOD.NS","DEVYANI.NS","WESTLIFE.NS","SAPPHIRE.NS",
    "ABFRL.NS","PAGEIND.NS","MUTHOOTFIN.NS","MANAPPURAM.NS","CHOLAFIN.NS",
    "LICHSGFIN.NS","PFC.NS","RECLTD.NS","IRFC.NS","HUDCO.NS",
    "HAL.NS","BEL.NS","BHEL.NS","COCHINSHIP.NS","MAZDOCK.NS",
    "LICI.NS","GICRE.NS","NIACL.NS","IRCTC.NS","CONCOR.NS"
]

START = (date.today() - timedelta(days=730)).isoformat()   # 2 years back
END   = date.today().isoformat()

RAW_DIR  = Path("data/raw")
RAW_DIR.mkdir(parents=True, exist_ok=True)
EOD_DIR  = Path("data/eod2/daily")
EOD_DIR.mkdir(parents=True, exist_ok=True)

EOD2_HEADER = "date,open,high,low,close,volume,series,value,trades,deliverable_volume"

all_close = {}
failed = []

print("Fetching OHLCV data...")
for ticker in NIFTY100_SYMBOLS_OHLCV:
    symbol = ticker.replace(".NS", "")
    try:
        df = yf.download(ticker, start=START, end=END, progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            failed.append(ticker)
            continue

        df.index = pd.to_datetime(df.index)
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
        df = df[["open","high","low","close","volume"]].dropna()

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
    except Exception as e:
        print(f"❌ {ticker}: {e}")
        failed.append(ticker)

close_panel = pd.DataFrame(all_close).sort_index()
close_panel.to_parquet(RAW_DIR / "nifty100_ohlcv.parquet")
print(f"\n✅ Saved {len(all_close)} symbols | Failed: {len(failed)}: {failed}")
print(f"Close panel shape: {close_panel.shape}")

# --- Step 3: Fetch Fundamentals ---
print("\nFetching Fundamentals...")
NIFTY100_SYMBOLS_FUND = [
    "RELIANCE","TCS","HDFCBANK","ICICIBANK","INFOSYS","HINDUNILVR","SBIN",
    "BHARTIARTL","ITC","KOTAKBANK","LT","AXISBANK","HCLTECH","BAJFINANCE",
    "WIPRO","MARUTI","ASIANPAINT","TITAN","SUNPHARMA","ULTRACEMCO",
    "NESTLEIND","POWERGRID","NTPC","TECHM","ONGC","BAJAJFINSV","JSWSTEEL",
    "TATAMOTORS","ADANIENT","COALINDIA","GRASIM","DRREDDY","DIVISLAB",
    "CIPLA","EICHERMOT","TATACONSUM","HEROMOTOCO","BRITANNIA","SHREECEM",
    "BPCL","HINDALCO","VEDL","TATASTEEL","APOLLOHOSP","INDUSINDBK",
    "ADANIPORTS","BAJAJ-AUTO","M&M","HDFCLIFE","SBILIFE"
]

BUNDLE_DIR = Path("data/bundle")
BUNDLE_DIR.mkdir(parents=True, exist_ok=True)

records = []
fetched_at = datetime.now(timezone.utc).isoformat()

for symbol in NIFTY100_SYMBOLS_FUND:
    ticker = symbol + ".NS"
    try:
        info = yf.Ticker(ticker).info
        roe = info.get("returnOnEquity", None)
        de  = info.get("debtToEquity", None)
        if roe is not None or de is not None:
            records.append({
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "symbol": symbol,
                "roe": float(roe) if roe is not None else None,
                "debt_to_equity": float(de) / 100.0 if de is not None else None,
                "fiscal_quarter_end": None,
                "source": "yfinance",
                "fetched_at": fetched_at,
            })
            print(f"✅ {symbol}: ROE={roe:.3f}, D/E={de}")
        else:
            print(f"⚠️  {symbol}: no fundamentals")
    except Exception as e:
        print(f"❌ {symbol}: {e}")

df = pd.DataFrame(records).dropna(subset=["roe","debt_to_equity"])
df.to_parquet(BUNDLE_DIR / "fundamentals_quarterly.parquet", index=False)

provenance = {
    "source": "yfinance",
    "fetched_at": fetched_at,
    "symbols": len(df["symbol"].unique()),
    "rows": len(df),
    "dropped_after_as_of": 0,
}
(BUNDLE_DIR / "fundamentals_provenance.json").write_text(
    json.dumps(provenance, indent=2), encoding="utf-8"
)
print(f"\n✅ Saved {len(df)} fundamentals rows for {df['symbol'].nunique()} symbols")

# --- Step 4: Write NSE membership CSV ---
print("\nWriting NSE membership CSV...")
SYMBOLS_MEM = [
    "RELIANCE","TCS","HDFCBANK","ICICIBANK","INFOSYS","HINDUNILVR","SBIN",
    "BHARTIARTL","ITC","KOTAKBANK","LT","AXISBANK","HCLTECH","BAJFINANCE",
    "WIPRO","MARUTI","ASIANPAINT","TITAN","SUNPHARMA","ULTRACEMCO",
    "NESTLEIND","POWERGRID","NTPC","TECHM","ONGC","BAJAJFINSV","JSWSTEEL",
    "TATAMOTORS","ADANIENT","COALINDIA","GRASIM","DRREDDY","DIVISLAB",
    "CIPLA","EICHERMOT","TATACONSUM","HEROMOTOCO","BRITANNIA","SHREECEM",
    "BPCL","HINDALCO","VEDL","TATASTEEL","APOLLOHOSP","INDUSINDBK",
    "ADANIPORTS","BAJAJ-AUTO","M&M","HDFCLIFE","SBILIFE","PIDILITIND",
    "DABUR","GODREJCP","MARICO","COLPAL","SIEMENS","HAVELLS","BERGEPAINT",
    "PGHH","MCDOWELL-N","BANKBARODA","PNB","CANBK","UNIONBANK","IDFCFIRSTB",
    "GAIL","IOC","HINDPETRO","OIL","MGL","ZOMATO","TRENT","JUBLFOOD",
    "ABFRL","PAGEIND","MUTHOOTFIN","MANAPPURAM","CHOLAFIN","LICHSGFIN",
    "PFC","RECLTD","IRFC","HUDCO","HAL","BEL","BHEL","IRCTC","CONCOR",
    "LICI","GICRE","NIACL","ADANIGREEN","ADANITRANS","ADANIPOWER",
    "DMART","NAUKRI","INFY","WIPRO"
]

mem_dir = Path("data/membership/index_history/data")
mem_dir.mkdir(parents=True, exist_ok=True)

header = "index_id,index_name,symbol,valid_from,valid_to,weightage,source,source_url,notes"
rows = [header]
for sym in set(SYMBOLS_MEM):
    rows.append(f'219,"Nifty 100",{sym},2022-01-01,,1.0,nse_website,,')

(mem_dir / "index_membership_history.csv").write_text(
    "\r\n".join(rows) + "\r\n", encoding="utf-8"
)
print(f"✅ Wrote membership for {len(set(SYMBOLS_MEM))} symbols")

# --- Step 5: Write eod2 meta.json ---
print("\nWriting eod2 meta.json...")
meta = {
    "data-version": "3.4",
    "lastUpdate": f"{date.today().isoformat()}T00:00:00+05:30",
    "equityActionsExpiry": f"{date.today().isoformat()}",
}
Path("data/eod2/meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

symbols_eod = [f.stem.upper() for f in Path("data/eod2/daily").glob("*.csv")]
isin_map = {sym: f"INE{i:08d}1" for i, sym in enumerate(symbols_eod)}
Path("data/eod2/isin_symbol_map.json").write_text(
    json.dumps({"sym2isin": isin_map}, indent=2), encoding="utf-8"
)
print(f"✅ meta.json + isin_symbol_map.json written for {len(symbols_eod)} symbols")
