# Diagnostics Module

This module implements **Section 0: Diagnostic Protocol** from the Quant India Dashboard specification.

## Overview

Before building the dashboard, run the 5-step diagnostic protocol to validate your backtesting harness:

1. **Buy-and-hold sanity check** - Backtest a pure long Nifty 50 position and compare against known history
2. **Zero-cost pass** - Re-run all 20 strategies with zero costs to check if signals exist
3. **Look-ahead check** - Verify signals at bar t only use data available as of t
4. **Universe check** - Confirm delisted/renamed stocks are in historical universe
5. **Cost breakdown** - Log gross P&L and total cost drag separately

Results are logged as **HYP-DIAG-001** in MLflow alongside the 20 original runs.

## Usage

### Run the full diagnostic protocol

```bash
# Basic usage (auto-discovers data files)
python diagnostics/run_diagnostics.py

# With explicit paths
python diagnostics/run_diagnostics.py \
    --index-data data/processed/nifty50.csv \
    --strategies-data reports/generated/experiments/experiments.jsonl \
    --output var/diagnostics/HYP-DIAG-001.json

# Skip MLflow logging
python diagnostics/run_diagnostics.py --no-mlflow

# Verbose output
python diagnostics/run_diagnostics.py --verbose
```

### Python API

```python
from diagnostics.protocol import DiagnosticProtocol, run_full_diagnostics

# Run with auto-discovery
result = run_full_diagnostics()

# Access results
print(result.overall_status)
print(result.summary)

# Or use the protocol directly
protocol = DiagnosticProtocol()
result = protocol.run_all(
    index_data=index_df,
    test_window=("2020-01-01", "2026-01-01"),
    strategies_data=strategies_list,
    strategy_signals=signals_list,
    historical_universe=universe_df,
    current_universe=current_stocks,
    strategy_results=results_list
)
```

## Output

Results are saved to:
- `var/diagnostics/HYP-DIAG-001.json` (JSON file)
- MLflow tracking (as experiment "diagnostics")

The JSON file contains:
- `hypothesis_id`: "HYP-DIAG-001"
- `timestamp`: When diagnostics were run
- `overall_status`: "passed", "failed", or "warning"
- `summary`: Human-readable summary
- `results`: Array of step results with status and details

## Interpretation

### If diagnostics FAIL:

1. **Buy-and-hold sanity check fails** → Your harness has a bug. Fix it before re-testing.
2. **Zero-cost pass fails** → No strategies show edge even without costs. The signal itself may not exist in your data. Check parameters, universe, or lookback bugs.
3. **Look-ahead check fails** → Signals use future data. This is the most common silent bug in home-built backtesters.
4. **Universe check fails** → Delisted stocks missing from historical data. This introduces survivorship bias.
5. **Cost breakdown shows all strategies with negative gross P&L** → The signal itself is wrong, not just the costs.

### If diagnostics PASS:

- Your harness is working correctly
- The 0/20 result is genuine (no edge after costs)
- This tells you that off-the-shelf retail strategies aren't your edge
- Consider lower-turnover asset-allocation approaches or differentiated research

## Module Structure

```
diagnostics/
├── __init__.py           # Public API exports
├── protocol.py           # Diagnostic protocol implementation
├── run_diagnostics.py   # CLI entry point
└── README.md             # This file
```

## Integration with Dashboard

The diagnostic results are displayed in:
- Command Center (Screen 1) - Shows overall diagnostic status
- Research Pipeline (Screen 2) - Context for strategy evaluation

The dashboard reads from `var/diagnostics/HYP-DIAG-001.json`.

## Priority

As stated in Section 6 of the specification:
> Run the diagnostic protocol in §0 — this is higher-value right now than any dashboard work

**Run diagnostics first, then build the dashboard.**
