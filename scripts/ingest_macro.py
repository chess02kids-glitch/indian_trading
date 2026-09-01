"""Fetch Indian/global macro series from FRED into ``data/raw``.

Credentials: FRED requires a per-user API key. It is **never** stored in this
repository — supply it through the environment::

    export FRED_API_KEY=...

AUDIT-002: a real FRED API key was committed here. It has been removed from
the source; treat the historical key as compromised and rotate it at
https://fred.stlouisfed.org/docs/api/api_key.html. Removing the value from the
working tree does not remove it from git history — rotation is mandatory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import requests


class MissingCredential(RuntimeError):
    """Raised when a required API credential is absent from the environment."""


def _fred_api_key() -> str:
    key = (os.getenv("FRED_API_KEY") or "").strip()
    if not key:
        raise MissingCredential(
            "FRED_API_KEY is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html and export it "
            "before running this script."
        )
    return key


# FRED Series IDs for India and global macro
SERIES = {
    "cpi": "INDCPIALLMINMEI",  # Consumer Price Index: All Items for India
    "inr_usd": "DEXINUS",  # India / U.S. Foreign Exchange Rate (Daily)
    "crude_price": "DCOILWTICO",  # Crude Oil Prices: West Texas Intermediate (WTI)
    "10y_gilt_yield": "INDIRLTLT01STM",  # Long-Term Interest Rates for India (Monthly)
    "repo_rate": "INTDSRINM193N",  # Interest Rates, Discount Rate for India
}


def fetch_series(series_id):
    url = (
        f"https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={_fred_api_key()}&file_type=json"
    )
    response = requests.get(url, timeout=30)
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
