"""Run a broad discovery sweep over the local EOD2 NSE panel.

Usage:
    python -m research.explorer.run_discovery [--start 2023-01-01] [--end 2026-08-31]

Writes:
    data/features/discovery_sweep.csv
    data/features/discovery_sweep.json   (full machine-readable report)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .io import (
    load_eod_panels,
    load_pit_universe,
    panel_universe_mask,
    resolve_research_universe,
)
from .sim import DiscoveryConfig
from .strategies import build_specs, run_specs

ROOT = Path(__file__).resolve().parents[2]
FEATURES_DIR = ROOT / "data" / "features"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-08-31")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-history", type=int, default=0)
    args = parser.parse_args(argv)

    print("Loading panels ...")
    panels = load_eod_panels(force=args.force)
    close = panels["close"]

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    close = close.loc[(close.index >= start) & (close.index <= end)]
    panels = {k: v.reindex(index=close.index, columns=close.columns) for k, v in panels.items()}

    # Universe mask from PIT membership; restrict to local symbols.
    universe = load_pit_universe()
    available_cols = resolve_research_universe(close, universe)
    print(
        f"Panel: {close.shape[0]} dates x {close.shape[1]} symbols over "
        f"{close.index.min().date()} to {close.index.max().date()}"
    )
    print(f"Using PIT-active symbols with local price data: {len(available_cols)}")

    # Common-price panel; rows with fewer than some history should not trade.
    close = close.loc[:, available_cols]
    panels = {k: v.loc[:, available_cols].reindex(index=close.index) for k, v in panels.items()}

    mask = panel_universe_mask(universe, close.index, close.columns)

    # Discovery config: India base one-way cost. The production engine uses a
    # more granular model; here we use a single conservative 12 bps one-way.
    config = DiscoveryConfig(rebalance_frequency="M", one_way_cost_bps=12.0)

    specs = build_specs()
    print(f"Running {len(specs)} strategy candidates ...")
    results = run_specs(panels, specs, config=config, mask=mask)
    results.to_csv(FEATURES_DIR / "discovery_sweep.csv")

    report = {
        "dataset": "eod2_data/NSE monthly parquet",
        "source_file_count": "5686 parquet files",
        "start": str(start.date()),
        "end": str(end.date()),
        "symbols": len(available_cols),
        "dates": len(close.index),
        "cost_model": {
            "one_way_bps": config.one_way_cost_bps,
            "note": "single conservative proportional cost for discovery",
        },
        "universe": "nifty100-pit membership masked to locally available symbols",
        "lookahead": "target weights shifted one row before returns",
        "results": results.reset_index().to_dict(orient="records"),
    }
    (FEATURES_DIR / "discovery_sweep.json").write_text(
        json.dumps(report, indent=2, default=str)
    )
    print(f"\nWrote {FEATURES_DIR / 'discovery_sweep.csv'}")

    cols = [
        "family",
        "cagr",
        "volatility",
        "sharpe",
        "sortino",
        "max_drawdown",
        "win_rate",
        "total_return",
    ]
    printable = results.loc[:, [c for c in cols if c in results.columns]].sort_values(
        "sharpe", ascending=False
    )
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    print(printable.head(60).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
