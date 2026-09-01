#!/usr/bin/env python
"""Expand the trading universe from the raw NSE EOD mirror already in this repo.

The clean parquet bundle in ``data/clean/eod2_data`` holds ~133 names. The raw
mirror in ``data/eod2/daily`` holds ~3,700. This script promotes more of them
into ``var/cache/broad_universe.parquet``, which :mod:`datahub.panel` merges
into the panel automatically.

Nothing new is downloaded and nothing new is committed: the raw files are
already in git, and the cache is derived data under the gitignored ``var/``.

Examples
--------
    # match the validated research universe (~550 liquid names, >=8y, >=Rs 1cr/day)
    python scripts/expand_universe.py

    # a bigger, looser universe
    python scripts/expand_universe.py --min-years 5 --min-value 3000000

    # just these names
    python scripts/expand_universe.py --symbols ZOMATO,TRENT,POLICYBZR

    # cap the size
    python scripts/expand_universe.py --limit 400

After it finishes, the Strategy / Data pages pick the new names up on the next
request (or click "Recompute signal").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--min-years",
        type=float,
        default=8.0,
        help="minimum years of history (default 8, matching the research card)",
    )
    parser.add_argument(
        "--min-value",
        type=float,
        default=10_000_000.0,
        help="minimum median daily traded value in INR (default 1 crore)",
    )
    parser.add_argument(
        "--start",
        default="2010-01-01",
        help="evaluation window start (default 2010-01-01)",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after N symbols")
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="comma-separated explicit symbol list (skips the liquidity scan)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="only report what would be promoted"
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)

    from datahub import universe

    if args.dry_run:
        candidates = universe.scan_candidates()
        print(f"{len(candidates)} raw symbols are not yet in the clean bundle")
        for row in candidates[:25]:
            print(
                f"  {row['symbol']:<18} ~{row['est_bars']:>6} bars  {row['bytes'] / 1024:.0f} KB"
            )
        if len(candidates) > 25:
            print(f"  ... and {len(candidates) - 25} more")
        return 0

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else None
    )
    result = universe.build_broad(
        min_years=args.min_years,
        min_avg_value=args.min_value,
        start=args.start,
        limit=args.limit,
        symbols=symbols,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0 if not result.get("error") else 1

    if result.get("error"):
        print(f"FAILED: {result['error']}")
        return 1
    print(
        f"promoted {result['accepted']} symbols -> {result['cache']}\n"
        f"  rows        {result.get('rows'):,}\n"
        f"  date range  {result.get('date_range')}\n"
        f"  cache size  {result.get('cache_mb')} MB\n"
        f"  seconds     {result.get('seconds')}\n"
        f"  skipped     {result.get('skipped')}"
    )
    from datahub.panel import clear_cache, data_status, materialize_prices

    clear_cache()
    prices = materialize_prices(force=True)
    status = data_status(refresh=True)
    uni = status.get("universe") or {}
    print(
        f"\nuniverse now {uni.get('size')} names "
        f"(panel {status.get('prices_info', {}).get('symbols')}) · "
        f"prices.parquet {prices.get('size_mb')} MB"
    )
    print("Open the dashboard and click 'Recompute signal' (or just reload).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
