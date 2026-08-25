"""Authentication CLI Commands for Quant India."""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Optional

from auth.health import AuthHealthMonitor
from auth.session import SessionManager
from observability.logging import get_logger

logger = get_logger("quant_india.auth.cli")


def build_auth_parser(
    subparsers: Optional[argparse._SubParsersAction] = None,
) -> argparse.ArgumentParser:
    """Build or attach the auth CLI parser."""
    if subparsers:
        parser = subparsers.add_parser(
            "auth", help="Manage broker authentication and secrets"
        )
    else:
        parser = argparse.ArgumentParser(description="Quant India Auth CLI")

    auth_subparsers = parser.add_subparsers(dest="auth_command", help="Auth commands")

    # Upstox Login
    auth_subparsers.add_parser("upstox", help="Initiate Upstox OAuth login")

    # Dhan Login
    auth_subparsers.add_parser("dhan", help="Initiate Dhan authentication")

    # Status
    auth_subparsers.add_parser(
        "status", help="Check current session and connectivity status"
    )

    # Validate
    auth_subparsers.add_parser(
        "validate", help="Validate static IPs and environment secrets"
    )

    return parser


def handle_login(broker: str) -> int:
    """Handle the interactive OAuth flow in the terminal."""
    try:
        manager = SessionManager()
        flow = manager.get_flow(broker)

        if not flow.is_configured:
            print(
                f"Error: {broker.capitalize()} credentials are not configured in the environment."
            )
            return 1

        # Generate URL
        login_url = flow.generate_login_url(state="cli_login")

        print(f"\n--- {broker.capitalize()} Authentication ---")
        print("Please visit the following URL to authorize the application:\n")
        print(f"{login_url}\n")

        # We prompt the user to paste the callback URL containing the code
        print("After authorization, you will be redirected to a localhost URL.")
        callback_url = input("Paste the ENTIRE redirected URL here: ").strip()

        if not callback_url:
            print("Operation cancelled.")
            return 1

        # Extract code from URL (basic parsing for MVP)
        from urllib.parse import parse_qs, urlparse

        parsed_url = urlparse(callback_url)
        query_params = parse_qs(parsed_url.query)

        code = query_params.get("code", [None])[0]
        if not code:
            print("Error: No 'code' parameter found in the provided URL.")
            return 1

        print("Exchanging code for token...")
        manager.login(broker, code)
        print("Login successful! Secure token has been encrypted and stored locally.")
        return 0

    except Exception as e:
        logger.exception(f"Login failed: {e}")
        print(f"Login failed: {e}")
        return 1


def handle_status() -> int:
    """Run health checks and print status."""
    monitor = AuthHealthMonitor()
    report = monitor.run_full_diagnostics()

    print("\n=== Quant India Auth Status ===")
    print(json.dumps(report, indent=2))
    return 0


def handle_validate() -> int:
    """Run strict environment validation."""
    from auth.secrets import secrets

    print("\n=== Validating Infrastructure ===")
    ok = secrets.verify_startup()
    if ok:
        print("[OK] Startup configuration verified.")
    else:
        print("[FAIL] Missing critical encryption or broker configuration.")

    ip = "127.0.0.1"
    ip_ok = secrets.validate_ip(ip)
    if ip_ok:
        print(f"[OK] Static IP {ip} is whitelisted.")
    else:
        print(f"[FAIL] Static IP {ip} is NOT whitelisted.")

    return 0 if ok else 1


def auth_cli_main(argv: Optional[Sequence[str]] = None) -> int:
    """Execute the auth CLI subcommands."""
    parser = build_auth_parser()

    # If this is called from the root CLI, the parent already parsed 'auth'.
    # We re-parse or we just parse the sub-arguments.
    # To keep it completely isolated (as requested), we parse argv directly.
    args = parser.parse_args(argv)

    # if auth_cli_main was passed argv starting with "auth", we'd skip it,
    # but build_auth_parser as root doesn't expect "auth" as the first arg unless attached.
    # Let's assume argv is exactly ["upstox"] or ["status"]
    if not hasattr(args, "auth_command") or not args.auth_command:
        parser.print_help()
        return 0

    if args.auth_command == "upstox":
        return handle_login("upstox")
    elif args.auth_command == "dhan":
        return handle_login("dhan")
    elif args.auth_command == "status":
        return handle_status()
    elif args.auth_command == "validate":
        return handle_validate()

    return 1


if __name__ == "__main__":
    sys.exit(auth_cli_main())
