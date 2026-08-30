from pathlib import Path

import pandas as pd
import yfinance as yf


def main():
    print("Fetching Corporate Actions (Dividends, Splits)...")
    with open("data/universe/nifty100_symbols.txt", "r") as f:
        symbols = [s.strip() for s in f.readlines() if s.strip()]

    all_actions = []

    for i, symbol in enumerate(symbols):
        if symbol == "symbol":
            continue
        ticker = f"{symbol}.NS"
        try:
            stock = yf.Ticker(ticker)
            actions = stock.actions
            if actions is not None and not actions.empty:
                actions = actions.copy()
                actions.reset_index(inplace=True)
                actions["symbol"] = symbol
                all_actions.append(actions)

            if i % 10 == 0:
                print(f"  fetched {i}/{len(symbols)}...")
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")

    if all_actions:
        df = pd.concat(all_actions, ignore_index=True)
        # Rename columns Date to date, Dividends to dividend, Stock Splits to split
        df.rename(
            columns={"Date": "date", "Dividends": "dividend", "Stock Splits": "split"},
            inplace=True,
        )
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)

        # We need to drop the implicit Capital Gains column if it exists and we only want div and split
        keep_cols = ["date", "symbol", "dividend", "split"]
        df = df[[c for c in keep_cols if c in df.columns]]

        out_path = Path("data/corporate_actions.csv")
        df.to_csv(out_path, index=False)
        print(f"Successfully wrote {len(df)} corporate action rows to {out_path}")
    else:
        print("No corporate actions found.")


if __name__ == "__main__":
    main()
