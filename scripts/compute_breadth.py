from pathlib import Path

import pandas as pd


def main():
    print("Computing Market Breadth from EOD prices...")

    clean_dir = Path("data/clean/eod2_data")
    if not clean_dir.exists():
        print(f"Error: {clean_dir} does not exist.")
        return

    all_closes = []

    # We only care about the Nifty 100 symbols for breadth
    with open("data/universe/nifty100_symbols.txt", "r") as f:
        symbols = [
            s.strip() for s in f.readlines() if s.strip() and s.strip() != "symbol"
        ]

    for symbol in symbols:
        parquet_path = clean_dir / f"{symbol}.parquet"
        if parquet_path.exists():
            try:
                df = pd.read_parquet(parquet_path, columns=["Close"])
                df.rename(columns={"Close": symbol}, inplace=True)
                all_closes.append(df)
            except Exception as e:
                print(f"Error reading {symbol}: {e}")

    if not all_closes:
        print("No price data found to compute breadth.")
        return

    # Merge all close prices
    merged = pd.concat(all_closes, axis=1)

    # Calculate daily returns
    returns = merged.pct_change()

    # Compute advances and declines
    advances = (returns > 0).sum(axis=1)
    declines = (returns < 0).sum(axis=1)
    unchanged = (returns == 0).sum(axis=1)

    breadth_df = pd.DataFrame(
        {"advances": advances, "declines": declines, "unchanged": unchanged}
    )

    # Add Advance-Decline Ratio (ADR)
    breadth_df["adr"] = breadth_df["advances"] / breadth_df["declines"].replace(0, 1)

    breadth_df.index.name = "date"
    breadth_df.reset_index(inplace=True)

    # Drop rows where everything is 0 (weekends/holidays)
    breadth_df = breadth_df[(breadth_df["advances"] > 0) | (breadth_df["declines"] > 0)]

    # Ensure timezone naive
    if breadth_df["date"].dt.tz is not None:
        breadth_df["date"] = breadth_df["date"].dt.tz_localize(None)

    # Sort
    breadth_df.sort_values("date", inplace=True)

    out_dir = Path("data/market")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "breadth.csv"
    breadth_df.to_csv(out_path, index=False)

    print(
        f"Successfully computed market breadth (A/D) for {len(breadth_df)} days -> {out_path}"
    )


if __name__ == "__main__":
    main()
