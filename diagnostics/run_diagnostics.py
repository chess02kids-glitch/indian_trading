#!/usr/bin/env python
"""Run the diagnostic protocol for Quant India trading system.

This script implements Section 0 of the dashboard specification.
It runs the 5-step diagnostic protocol and logs results as HYP-DIAG-001.

Usage:
    python diagnostics/run_diagnostics.py
    python -m diagnostics.run_diagnostics
"""

from __future__ import annotations

import argparse
from pathlib import Path

from diagnostics.protocol import run_full_diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run diagnostic protocol for Quant India trading system"
    )
    parser.add_argument(
        "--index-data",
        type=str,
        default=None,
        help="Path to Nifty 50 index data CSV",
    )
    parser.add_argument(
        "--strategies-data",
        type=str,
        default=None,
        help="Path to strategies data JSON/JSONL",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="var/diagnostics/HYP-DIAG-001.json",
        help="Output path for diagnostic results",
    )
    parser.add_argument(
        "--no-mlflow",
        action="store_true",
        help="Disable MLflow logging",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed output",
    )
    
    args = parser.parse_args()
    
    print("=" * 60)
    print("Quant India - Diagnostic Protocol (HYP-DIAG-001)")
    print("=" * 60)
    print()
    
    result = run_full_diagnostics(
        index_data_path=args.index_data,
        strategies_data_path=args.strategies_data,
        output_path=args.output,
        log_to_mlflow=not args.no_mlflow,
    )
    
    print(result.summary)
    print()
    print(f"Results saved to: {args.output}")
    print(f"Overall status: {result.overall_status}")
    
    if args.verbose:
        print("\nDetailed results:")
        for step_result in result.results:
            print(f"\n  [{step_result.status.value.upper()}] {step_result.step.value}")
            print(f"    Message: {step_result.message}")
            if step_result.details:
                print(f"    Details: {step_result.details}")


if __name__ == "__main__":
    main()
