"""Read-only Streamlit research dashboard.

Views:

* Research Leaderboard — strategy metrics ranked by deflated-Sharpe evidence;
* Validation Dashboard — walk-forward / CPCV fold consistency;
* Factor Diagnostics — factor decay, rank stability, sector exposure;
* Gate History — every research-gate verdict with its reasons;
* Experiment Timeline — successful, rejected, failed, and interrupted runs;
* Benchmark Comparison — candidate versus buy-and-hold, equal weight,
  inverse volatility, persistence, and placebo.

The dashboard is strictly read-only: it loads JSON/JSONL artifacts produced
by the research workflow and contains no control for placing, amending, or
cancelling orders. It is not an execution panel.

Run with::

    streamlit run dashboard/research_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_EXPERIMENT_PATH = "reports/generated/experiments/experiments.jsonl"
DEFAULT_GATE_PATH = "reports/generated/research_gate.json"
DEFAULT_GATE_SUMMARY = "reports/generated/research_gate_summary.json"
DEFAULT_DIAGNOSTICS_PATH = "reports/generated/factor_diagnostics.json"
DEFAULT_REPORT_PATH = "reports/generated/momentum_quality.json"
DEFAULT_ADVANCED_PATH = "reports/generated/momentum_quality_research_monthly.json"


def load_json(path: str | Path) -> dict[str, Any] | None:
    """Load one JSON object, returning None when missing or malformed."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load JSONL records, skipping malformed lines without failing."""
    path = Path(path)
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def leaderboard_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce experiment records to a sortable research leaderboard.

    Rows are ordered by deflated-Sharpe probability (desc); rows without a
    probability sort last. Surviorship-safe: rejected, failed, and
    interrupted records remain visible with their status.
    """
    rows: list[dict[str, Any]] = []
    for record in records:
        metrics = record.get("metrics") or {}
        validation = record.get("validation") or {}
        dsr = validation.get("deflated_sharpe") or {}
        rows.append(
            {
                "hypothesis_id": record.get("hypothesis_id"),
                "strategy": record.get("strategy"),
                "status": record.get("status"),
                "sharpe": metrics.get("sharpe"),
                "deflated_sharpe_probability": (
                    dsr.get("probability")
                    if isinstance(dsr, dict)
                    else metrics.get("deflated_sharpe_probability")
                ),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "turnover": metrics.get("turnover"),
                "recorded_at": record.get("ended_at"),
                "reason": record.get("reason"),
            }
        )
    rows.sort(
        key=lambda row: (
            row["deflated_sharpe_probability"] is None,
            -(row["deflated_sharpe_probability"] or 0.0),
            row["hypothesis_id"] or "",
        )
    )
    return rows


def gate_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten gate decisions stored inside experiment records."""
    history: list[dict[str, Any]] = []
    for record in records:
        gate = record.get("gate_result") or {}
        if not gate:
            continue
        history.append(
            {
                "hypothesis_id": record.get("hypothesis_id"),
                "strategy": record.get("strategy"),
                "verdict": gate.get("verdict"),
                "score": gate.get("score"),
                "check_failures": [
                    check.get("name")
                    for check in (gate.get("checks") or [])
                    if check.get("status") == "fail"
                ],
                "generated_at": gate.get("generated_at") or record.get("ended_at"),
            }
        )
    history.sort(key=lambda row: row.get("generated_at") or "")
    return history


def validation_frame(validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Reduce a validation payload (walk-forward or CPCV) to fold metrics."""
    if not validation:
        return []
    folds = validation.get("fold_metrics") or []
    windows = validation.get("windows") or []
    rows: list[dict[str, Any]] = []
    for index, metrics in enumerate(folds):
        window = windows[index] if index < len(windows) else {}
        rows.append(
            {
                "fold": metrics.get("observations", index),
                "sharpe": metrics.get("sharpe"),
                "total_return": metrics.get("total_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "observations": metrics.get("observations"),
                "test_start": (window or {}).get("test_start"),
                "test_end": (window or {}).get("test_end"),
            }
        )
    return rows


def factor_diagnostics_view(
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reduce factor diagnostics into dashboard-friendly blocks."""
    if not diagnostics:
        return {}
    return {
        "factor_decay": diagnostics.get("factor_decay") or {},
        "rank_stability": diagnostics.get("rank_stability") or {},
        "sector_exposure": diagnostics.get("sector_exposure") or {},
        "turnover_attribution": diagnostics.get("turnover_attribution") or {},
        "volatility_contribution": diagnostics.get("volatility_contribution") or {},
    }


def benchmark_comparison_view(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Reduce a research report's benchmark comparison to a table."""
    if not report:
        return []
    comparison = report.get("benchmark_comparison") or {}
    rows: list[dict[str, Any]] = []
    for name, values in comparison.items():
        rows.append(
            {
                "name": name,
                "sharpe": values.get("sharpe"),
                "annualized_return": values.get("annualized_return"),
                "max_drawdown": values.get("max_drawdown"),
                "volatility": values.get("annualized_volatility"),
                "turnover": values.get("turnover"),
            }
        )
    rows.sort(key=lambda row: row["name"] or "")
    return rows


def timeline_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ordered experiment timeline (oldest first) with all outcomes."""
    return sorted(
        records,
        key=lambda record: (record.get("started_at") or "", record.get("run_id") or ""),
    )


def render(
    *,
    experiment_path: str | Path = DEFAULT_EXPERIMENT_PATH,
    gate_summary_path: str | Path = DEFAULT_GATE_SUMMARY,
    diagnostics_path: str | Path = DEFAULT_DIAGNOSTICS_PATH,
    report_path: str | Path = DEFAULT_REPORT_PATH,
    advanced_path: str | Path = DEFAULT_ADVANCED_PATH,
) -> None:
    """Render the research dashboard (called by ``streamlit run``)."""
    from dashboard.streamlit_guard import require_streamlit as _require_streamlit

    st = _require_streamlit()

    st.set_page_config(page_title="Quant India — Research", layout="wide")
    st.title("Quant India — Research Dashboard")
    st.caption(
        "Read-only research evidence. No order, execution, or broker controls "
        "exist on this page."
    )

    records = load_jsonl(experiment_path)
    gate_summary = load_json(gate_summary_path)
    diagnostics = load_json(diagnostics_path)
    report = load_json(report_path)
    advanced = load_json(advanced_path)

    st.header("Research Leaderboard")
    rows = leaderboard_records(records)
    if rows:
        st.dataframe(rows)
    else:
        st.info("No experiment records found yet.")

    st.header("Validation Dashboard")
    validation = (
        (gate_summary or {}).get("validation")
        or (advanced or {}).get("validation")
        or {}
    )
    fold_rows = validation_frame(validation)
    if fold_rows:
        st.dataframe(fold_rows)
    else:
        st.info("No validation fold data found yet.")
    consistency = (gate_summary or {}).get("validation_consistency")
    if consistency:
        st.json(consistency)

    st.header("Factor Diagnostics")
    view = factor_diagnostics_view(diagnostics)
    if view:
        st.subheader("Factor decay (IC by horizon)")
        st.json(view["factor_decay"])
        st.subheader("Rank stability")
        st.json(view["rank_stability"])
        st.subheader("Volatility contribution")
        st.json(view["volatility_contribution"])
    else:
        st.info("No factor diagnostics found yet.")

    st.header("Gate History")
    history = gate_history(records)
    if history:
        st.dataframe(history)
    else:
        st.info("No gate decisions recorded yet.")

    st.header("Experiment Timeline")
    timeline = timeline_records(records)
    if timeline:
        st.dataframe(
            [
                {
                    "hypothesis_id": item.get("hypothesis_id"),
                    "status": item.get("status"),
                    "strategy": item.get("strategy"),
                    "started_at": item.get("started_at"),
                    "reason": item.get("reason"),
                }
                for item in timeline
            ]
        )
    else:
        st.info("No experiment timeline found yet.")

    st.header("Benchmark Comparison")
    benchmark_rows = benchmark_comparison_view(advanced or report)
    if benchmark_rows:
        st.dataframe(benchmark_rows)
        st.line_chart(
            {
                row["name"]: row["sharpe"]
                for row in benchmark_rows
                if row["sharpe"] is not None
            }
        )
    else:
        st.info("No benchmark comparison found yet.")


def main() -> None:
    """Entry point for ``streamlit run``."""
    render()


if __name__ == "__main__":
    main()
