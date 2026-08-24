import argparse
import sys
from typing import List

from data.duckdb_manager import DuckDBManager
from ingestion.pipeline import IngestionPipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quant India Data Platform CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest market data")
    ingest_parser.add_argument("--symbol", type=str, help="Single symbol to ingest (e.g., RELIANCE.NS)")
    ingest_parser.add_argument("--universe", type=str, help="Comma-separated list of symbols")
    ingest_parser.add_argument("--period", type=str, default="max", help="Period to fetch (default: max)")

    # Validate command
    subparsers.add_parser("validate", help="Run full database validation")

    # Snapshot command
    snapshot_parser = subparsers.add_parser("snapshot", help="Create a DuckDB snapshot")
    snapshot_parser.add_argument("--name", type=str, default="market_snapshot", help="Snapshot name")

    return parser.parse_args()


def main():
    """
    Quant India CLI Entry Point.
    Market and IOC orders are prohibited.
    Future execution must pass through risk_kill.
    """
    args = parse_args()

    if args.command == "ingest":
        pipeline = IngestionPipeline()
        if args.symbol:
            pipeline.ingest_symbol(args.symbol, period=args.period)
        elif args.universe:
            symbols = [s.strip() for s in args.universe.split(",")]
            pipeline.ingest_universe(symbols, period=args.period)
        else:
            print("Error: Must provide --symbol or --universe")
            sys.exit(1)

    elif args.command == "validate":
        print("Validation engine triggered (Schema validation happens during ingestion)")
        print("Full DB validation coming soon.")
        # We can implement a full db validation via DuckDB manager later

    elif args.command == "snapshot":
        db = DuckDBManager()
        path = db.create_snapshot(args.name)
        print(f"Snapshot created at {path}")

    else:
        print("Quant India system initialized. Safe no-op entry point.")
        print("Run with --help for available commands.")

if __name__ == "__main__":
    main()
