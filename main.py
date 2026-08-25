import argparse
import sys

from data.duckdb_manager import DuckDBManager
from ingestion.pipeline import IngestionPipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Quant India Data Platform CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest market data")
    ingest_parser.add_argument(
        "--symbol", type=str, help="Single symbol to ingest (e.g., RELIANCE.NS)"
    )
    ingest_parser.add_argument(
        "--universe", type=str, help="Comma-separated list of symbols"
    )
    ingest_parser.add_argument(
        "--period", type=str, default="max", help="Period to fetch (default: max)"
    )

    # Validate command
    subparsers.add_parser("validate", help="Run full database validation")

    # Snapshot command
    snapshot_parser = subparsers.add_parser("snapshot", help="Create a DuckDB snapshot")
    snapshot_parser.add_argument(
        "--name", type=str, default="market_snapshot", help="Snapshot name"
    )

    # Research command
    subparsers.add_parser("research", help="Run the research platform CLI")

    # Broker sandbox command
    subparsers.add_parser("broker", help="Broker sandbox operations (LIVE disabled)")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None):
    """
    Quant India CLI Entry Point.
    Market and IOC orders are prohibited.
    Future execution must pass through risk_kill.
    """
    argv_list = list(sys.argv[1:] if argv is None else argv)
    if argv_list and argv_list[0] == "broker":
        # Broker sandbox CLI owns its argument parsing (subcommands take
        # their own options, which the root parser would otherwise reject).
        from broker.cli import cli_main as broker_cli_main

        sys.exit(broker_cli_main(argv_list[1:]))

    args = parse_args(argv)

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
        print(
            "Validation engine triggered (Schema validation happens during ingestion)"
        )
        print("Full DB validation coming soon.")
        # We can implement a full db validation via DuckDB manager later

    elif args.command == "snapshot":
        db = DuckDBManager()
        path = db.create_snapshot(args.name)
        print(f"Snapshot created at {path}")

    elif args.command == "research":
        from research.cli import cli_main

        sys.argv.remove("research")
        sys.exit(cli_main())

    else:
        print("Quant India system initialized. Safe no-op entry point.")
        print("Run with --help for available commands.")


if __name__ == "__main__":
    main()
