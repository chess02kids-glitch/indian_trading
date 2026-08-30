"""Diagnostic protocol for Quant India trading system.

This module implements the diagnostic protocol from Section 0 of the dashboard
specification. It provides tools to validate the backtesting harness and identify
issues before building the dashboard.

Run with:
    python -m diagnostics.run_diagnostics
"""

from .protocol import (
    DiagnosticProtocol,
    DiagnosticResult,
    run_full_diagnostics,
)

__all__ = [
    "DiagnosticProtocol",
    "DiagnosticResult",
    "run_full_diagnostics",
]
