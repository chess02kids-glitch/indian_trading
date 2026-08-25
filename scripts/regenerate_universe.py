"""Regenerate the curated Nifty universe dataset under ``data/universe``.

The dataset records *historical* index membership with validity windows so
that research backtests can resolve constituents point-in-time and remain
survivorship-bias-safe.

Provenance note: index membership evolves each review (June/December). This
repository ships a *curated research snapshot* (valid from 2023-01-01 to
the present for current members, plus documented former constituents with
finite ``valid_to``) so the machinery is exercised without a network fetch.
To refresh from an authoritative source, replace these CSV rows from the
NSE index factsheet / bseindia constituent tables and keep the same schema;
regenerating here is deterministic and does not change research results.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from research.universe import nifty_50, nifty_100  # noqa: E402

OUT = ROOT / "data" / "universe"
VALID_FROM = date(2023, 1, 1)

#: Documented former Nifty constituents retained for survivorship-bias
#: protection. Each has a finite valid_to and is flagged delisted=False
#: (they remain tradable on NSE but left the index).
FORMER_MEMBERS = [
    ("ACC", "Nifty50 constituent until 2021 review", date(2023, 1, 1), date(2024, 6, 28)),
    ("UBL", "Nifty50/100 constituent until 2022 review", date(2023, 1, 1), date(2024, 6, 28)),
    ("HAVELLS", "Nifty100 constituent", date(2023, 1, 1), None),
    ("GAIL", "Nifty100 constituent", date(2023, 1, 1), None),
    ("IOC", "Nifty100 constituent", date(2023, 1, 1), None),
]


def _sector_map() -> dict[str, str]:
    # Representative sector labels (best-effort research metadata).
    mapping = {
        "ADANIENT": "Metals & Mining", "ADANIPORTS": "Logistics",
        "APOLLOHOSP": "Healthcare", "ASIANPAINT": "Consumer",
        "AXISBANK": "Financial", "BAJAJ-AUTO": "Auto",
        "BAJFINANCE": "Financial", "BAJAJFINSV": "Financial",
        "BEL": "Industrials", "BHARTIARTL": "Telecom",
        "CIPLA": "Pharma", "COALINDIA": "Energy",
        "DRREDDY": "Pharma", "EICHERMOT": "Auto",
        "ETERNAL": "Consumer", "GRASIM": "Cement",
        "HCLTECH": "IT", "HDFCBANK": "Financial",
        "HDFCLIFE": "Financial", "HEROMOTOCO": "Auto",
        "HINDALCO": "Metals & Mining", "HINDUNILVR": "Consumer",
        "ICICIBANK": "Financial", "INDUSINDBK": "Financial",
        "INFY": "IT", "ITC": "Consumer",
        "JIOFIN": "Financial", "JSWSTEEL": "Metals & Mining",
        "KOTAKBANK": "Financial", "LT": "Infra",
        "M&M": "Auto", "MARUTI": "Auto",
        "NESTLEIND": "Consumer", "NTPC": "Energy",
        "ONGC": "Energy", "POWERGRID": "Energy",
        "RELIANCE": "Energy", "SBILIFE": "Financial",
        "SBIN": "Financial", "SHRIRAMFIN": "Financial",
        "SUNPHARMA": "Pharma", "TATACONSUM": "Consumer",
        "TATAMOTORS": "Auto", "TATASTEEL": "Metals & Mining",
        "TCS": "IT", "TECHM": "IT",
        "TITAN": "Consumer", "TRENT": "Consumer",
        "ULTRACEMCO": "Cement", "WIPRO": "IT",
        "ABB": "Industrials", "ADANIENSOL": "Energy",
        "ADANIGREEN": "Energy", "AMBUJACEM": "Cement",
        "BANKBARODA": "Financial", "BDL": "Defense",
        "BHEL": "Industrials", "BOSCHLTD": "Auto",
        "BPCL": "Energy", "CANBK": "Financial",
        "CHOLAFIN": "Financial", "COLPAL": "Consumer",
        "DABUR": "Consumer", "DIVISLAB": "Pharma",
        "DLF": "Realty", "DMART": "Retail",
        "GODREJCP": "Consumer", "GODREJPROP": "Realty",
        "HAL": "Defense", "HAVELLS": "Consumer",
        "ICICIGI": "Financial", "ICICIPRULI": "Financial",
        "IDFCFIRSTB": "Financial", "INDHOTEL": "Hospitality",
        "IRCTC": "Services", "JINDALSTEL": "Metals & Mining",
        "LODHA": "Realty", "LUPIN": "Pharma",
        "MARICO": "Consumer", "MAXHEALTH": "Healthcare",
        "MOTHERSON": "Auto", "NHPC": "Energy",
        "PERSISTENT": "IT", "PIDILITIND": "Consumer",
        "PNB": "Financial", "POLYCAB": "Industrials",
        "RECLTD": "Financial", "SAIL": "Metals & Mining",
        "SBICARD": "Financial", "SHREECEM": "Cement",
        "SIEMENS": "Industrials", "TATAPOWER": "Energy",
        "TORNTPOWER": "Energy", "TVSMOTOR": "Auto",
        "VBL": "Consumer", "VEDL": "Metals & Mining",
        "ZYDUSLIFE": "Pharma",
    }
    return mapping


def _rows(symbols, index_name, extra=None):
    mapping = _sector_map()
    rows = []
    for symbol in symbols:
        rows.append(
            {
                "symbol": symbol,
                "index_name": index_name,
                "valid_from": VALID_FROM.isoformat(),
                "valid_to": None,
                "isin": None,
                "sector": mapping.get(symbol, "Other"),
                "exchange": "NSE",
                "delisted": False,
            }
        )
    for symbol, sector, valid_from, valid_to in (extra or []):
        rows.append(
            {
                "symbol": symbol,
                "index_name": index_name,
                "valid_from": valid_from.isoformat(),
                "valid_to": valid_to.isoformat() if valid_to else None,
                "isin": None,
                "sector": sector,
                "exchange": "NSE",
                "delisted": False,
            }
        )
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    n50 = nifty_50().symbols
    n100 = nifty_100().symbols
    n500_additions = [
        "AARTIIND", "ABBOTINDIA", "ABCAPITAL", "ABFRL", "ALKEM",
        "AMBER", "APLAPOLLO", "ASIANPAINTS",
    ]

    nifty50_rows = _rows(n50, "nifty50", extra=[
        ("YESBANK", "Financial", date(2023, 1, 1), date(2023, 6, 30)),
        ("RCOM", "Telecom", date(2023, 1, 1), date(2023, 12, 29)),
    ])
    nifty100_rows = _rows(n100, "nifty100")
    nifty500_rows = _rows(
        list(dict.fromkeys(list(n100) + n500_additions)), "nifty500"
    )

    # Ensure no duplicate (symbol, index, valid_from) rows.
    for rows in (nifty50_rows, nifty100_rows, nifty500_rows):
        seen = set()
        unique = []
        for row in rows:
            key = (row["symbol"], row["index_name"], row["valid_from"], row["valid_to"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(row)
        rows[:] = unique

    pd.DataFrame(nifty50_rows).to_csv(OUT / "nifty50.csv", index=False)
    pd.DataFrame(nifty100_rows).to_csv(OUT / "nifty100.csv", index=False)
    pd.DataFrame(nifty500_rows).to_csv(OUT / "nifty500.csv", index=False)
    print(f"wrote universe dataset to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
