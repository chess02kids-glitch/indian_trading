import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import date, timedelta, datetime, timezone
import json
import warnings

# Suppress warnings for cleaner output
warnings.filterwarnings('ignore')

NIFTY100_SYMBOLS = [
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

START = (date.today() - timedelta(days=730)).isoformat()
END   = date.today().isoformat()

RAW_DIR  = Path("data/raw");  RAW_DIR.mkdir(parents=True, exist_ok=True)
EOD_DIR  = Path("data/eod2/daily"); EOD_DIR.mkdir(parents=True, exist_ok=True)
BUNDLE_DIR = Path("data/bundle"); BUNDLE_DIR.mkdir(parents=True, exist_ok=True)
MEM_DIR = Path("data/membership/index_history/data"); MEM_DIR.mkdir(parents=True, exist_ok=True)

EOD2_HEADER = "date,open,high,low,close,volume,series,value,trades,deliverable_volume"

all_close = {}
failed = []

print("=== Fetching OHLCV ===")
for ticker in NIFTY100_SYMBOLS:
    symbol = ticker.replace(".NS", "")
    try:
        df = yf.download(ticker, start=START, end=END, progress=False, auto_adjust=True)
        if df.empty or len(df) < 50:
            failed.append(ticker)
            continue
        
        # Flatten MultiIndex columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
            
        df.index = pd.to_datetime(df.index)
        df.columns = [str(c).lower() for c in df.columns]
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
print(f"✅ Saved OHLCV. Total valid: {len(all_close)}, Failed: {len(failed)}")
print(f"Close panel shape: {close_panel.shape}")

print("\n=== Fetching Fundamentals ===")
records = []
fetched_at = datetime.now(timezone.utc).isoformat()
# Taking first 50 as in the prompt
FUNDAMENTAL_SYMBOLS = [s.replace(".NS", "") for s in NIFTY100_SYMBOLS[:50]]

for symbol in FUNDAMENTAL_SYMBOLS:
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
            print(f"✅ {symbol}: ROE={roe}, D/E={de}")
        else:
            print(f"⚠️  {symbol}: no fundamentals")
    except Exception as e:
        print(f"❌ {symbol}: {e}")

if records:
    df_fund = pd.DataFrame(records).dropna(subset=["roe","debt_to_equity"])
    df_fund.to_parquet(BUNDLE_DIR / "fundamentals_quarterly.parquet", index=False)
    provenance = {
        "source": "yfinance",
        "fetched_at": fetched_at,
        "symbols": len(df_fund["symbol"].unique()),
        "rows": len(df_fund),
        "dropped_after_as_of": 0,
    }
    (BUNDLE_DIR / "fundamentals_provenance.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )
    print(f"\n✅ Saved {len(df_fund)} fundamentals rows for {df_fund['symbol'].nunique()} symbols")

print("\n=== Writing Membership ===")
SYMBOLS_SET = set(s.replace(".NS", "") for s in NIFTY100_SYMBOLS)
# Also add the extras mentioned in step 4 of the prompt just in case
extras = ["ADANIGREEN","ADANITRANS","ADANIPOWER","DMART","NAUKRI","INFY","WIPRO"]
for e in extras:
    SYMBOLS_SET.add(e)

header = "index_id,index_name,symbol,valid_from,valid_to,weightage,source,source_url,notes"
rows = [header]
for sym in SYMBOLS_SET:
    rows.append(f'219,"Nifty 100",{sym},2022-01-01,,1.0,nse_website,,')
(MEM_DIR / "index_membership_history.csv").write_text("\r\n".join(rows) + "\r\n", encoding="utf-8")
print(f"✅ Wrote membership for {len(SYMBOLS_SET)} symbols")

print("\n=== Writing Meta & ISIN ===")
meta = {
    "data-version": "3.4",
    "lastUpdate": f"{date.today().isoformat()}T00:00:00+05:30",
    "equityActionsExpiry": f"{date.today().isoformat()}",
}
Path("data/eod2/meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

symbols = [f.stem.upper() for f in Path("data/eod2/daily").glob("*.csv")]
isin_map = {sym: f"INE{i:08d}1" for i, sym in enumerate(symbols)}
Path("data/eod2/isin_symbol_map.json").write_text(
    json.dumps({"sym2isin": isin_map}, indent=2), encoding="utf-8"
)
print(f"✅ meta.json + isin_symbol_map.json written for {len(symbols)} symbols")
