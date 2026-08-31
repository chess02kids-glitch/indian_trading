import json
import random
from pathlib import Path

import pandas as pd


def main():
    print("Generating synthetic quarterly fundamentals...")

    out_dir = Path("data/bundle")
    out_dir.mkdir(parents=True, exist_ok=True)

    # We load all symbols from the requested constituents
    symbols = set()
    universe_root = Path("data/universe")
    for slug in ("nifty50", "nifty100", "nifty500"):
        csv_path = universe_root / f"{slug}-pit" / f"{slug}.csv"
        if csv_path.exists():
            df = pd.read_csv(csv_path)
            symbols.update(df["symbol"].unique())

    if not symbols:
        print("No universe symbols found. Please run ingestion first.")
        return

    print(f"Loaded {len(symbols)} unique symbols.")

    # Generate quarterly dates from 2010 to 2026
    dates = pd.date_range(start="2010-01-01", end="2026-08-31", freq="QE")

    records = []

    # Deterministic seed for reproducible testing
    random.seed(42)  # nosec B311 - mock data generator, not security

    for symbol in sorted(symbols):
        # assign a random "baseline" profile to each stock
        base_roe = random.uniform(0.05, 0.25)  # nosec B311 - mock data generator, not security
        base_de = random.uniform(0.1, 2.0)  # nosec B311 - mock data generator, not security

        for date in dates:
            # Add some random walk noise
            roe = base_roe + random.uniform(-0.02, 0.02)  # nosec B311 - mock data generator, not security
            de = base_de + random.uniform(-0.1, 0.1)  # nosec B311 - mock data generator, not security

            # Bound them realistically
            roe = max(0.01, min(roe, 0.40))
            de = max(0.0, min(de, 5.0))

            records.append(
                {"date": date, "symbol": symbol, "roe": roe, "debt_to_equity": de}
            )

    df = pd.DataFrame(records)
    # the index_name needs to be None for to_parquet
    out_path = out_dir / "fundamentals_quarterly.parquet"
    df.to_parquet(out_path, index=False)
    print(f"Saved {len(df)} fundamental rows to {out_path}")

    # Write provenance
    provenance = {
        "source": "synthetic_mock",
        "description": "Mocked quarterly fundamentals (ROE and Debt-to-Equity) for baseline backtests. Generated offline due to network limits.",
        "rows": len(df),
        "symbols": len(symbols),
        "date_range": ["2010-01-01", "2026-08-31"],
    }
    with open(out_dir / "fundamentals_provenance.json", "w") as f:
        json.dump(provenance, f, indent=2)


if __name__ == "__main__":
    main()
