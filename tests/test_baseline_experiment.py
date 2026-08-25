"""End-to-end tests for the baseline research experiment script.

These tests run ``scripts/run_research_experiment.py`` twice on identical
inputs and verify the reproducibility contract of the whole pipeline:

* the same data, universe, strategy, cost configuration, seed, and code
  version produce the same research result;
* the locked holdout boundaries are recorded explicitly in the report,
  the gate summary, and the ledger;
* rejected experiments keep their gate reason as a first-class record;
* the cost scenarios (optimistic/base/pessimistic) are identified and
  ordered correctly.

The script is deterministic by construction (seeded synthetic data, no
network, no wall-clock values in research metrics), so two runs must
agree on every research field. Only identity/timestamp fields (run ids,
generated_at, absolute artifact paths) are excluded from comparison.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_research_experiment.py"
# 580 periods -> 328 development observations (one walk-forward fold of
# 252+5+63) and the standard 252-observation locked holdout.
PERIODS = "580"


@pytest.fixture(scope="module")
def experiment_module():
    spec = importlib.util.spec_from_file_location(
        "run_research_experiment_under_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _canonical_summary(path: Path) -> dict:
    """Load the experiment summary with volatile fields removed."""
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary.pop("generated_at", None)
    record = summary.get("record") or {}
    for volatile in ("started_at", "ended_at", "run_id", "artifacts"):
        record.pop(volatile, None)
    record_gate = record.get("gate_result") or {}
    record_gate.pop("generated_at", None)
    gate = summary.get("research_gate") or {}
    gate.pop("generated_at", None)
    for key in (
        "reports",
        "periodic_reports",
        "advanced_reports",
        "factor_diagnostics",
    ):
        summary.pop(key, None)
    # Artifact *names* and experiment ids are deterministic; absolute paths
    # are not. Keep the names for comparison, drop the paths.
    artifacts = summary.get("artifacts") or {}
    summary.pop("artifacts", None)
    summary["artifact_names"] = sorted(artifacts)
    return summary


def _gate_summary(path: Path) -> dict:
    gate = json.loads(path.read_text(encoding="utf-8"))
    gate.pop("path", None)
    return gate


def _ledger_records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _ledger_canonical(record: dict) -> dict:
    canonical = dict(record)
    canonical.pop("recorded_at", None)
    canonical.pop("run_id", None)
    gate_result = dict(canonical.get("gate_result") or {})
    gate_result.pop("generated_at", None)
    canonical["gate_result"] = gate_result
    return canonical


def test_baseline_experiment_is_deterministic(experiment_module, tmp_path) -> None:
    first_dir = tmp_path / "run1"
    second_dir = tmp_path / "run2"
    assert (
        experiment_module.main(["--output-dir", str(first_dir), "--periods", PERIODS])
        == 0
    )
    assert (
        experiment_module.main(["--output-dir", str(second_dir), "--periods", PERIODS])
        == 0
    )

    first = _canonical_summary(first_dir / "baseline_experiment_summary.json")
    second = _canonical_summary(second_dir / "baseline_experiment_summary.json")
    assert first == second

    first_gate = _gate_summary(first_dir / "research_gate_summary.json")
    second_gate = _gate_summary(second_dir / "research_gate_summary.json")
    assert first_gate == second_gate

    first_ledger = [
        _ledger_canonical(record)
        for record in _ledger_records(first_dir / "experiments" / "ledger.jsonl")
    ]
    second_ledger = [
        _ledger_canonical(record)
        for record in _ledger_records(second_dir / "experiments" / "ledger.jsonl")
    ]
    assert first_ledger == second_ledger
    assert len(first_ledger) == 1


def test_locked_holdout_is_recorded_end_to_end(experiment_module, tmp_path) -> None:
    output_dir = tmp_path / "run"
    assert (
        experiment_module.main(["--output-dir", str(output_dir), "--periods", PERIODS])
        == 0
    )
    summary = json.loads(
        (output_dir / "baseline_experiment_summary.json").read_text(encoding="utf-8")
    )
    boundaries = summary["holdout_boundaries"]
    # Explicit, chronological, non-overlapping partition.
    assert boundaries["dev_end"] < boundaries["holdout_start"]
    assert int(boundaries["dev_size"]) + int(boundaries["holdout_size"]) == int(PERIODS)
    assert summary["holdout_period"].startswith(boundaries["holdout_start"][:10])
    assert summary["dev_period"].endswith(boundaries["dev_end"][:10])
    # The out-of-sample evidence IS the locked holdout, not an overlapping
    # slice of the walk-forward timeline.
    assert summary["oos_period"] == summary["holdout_period"]

    gate = json.loads((output_dir / "research_gate_summary.json").read_text())
    assert gate["holdout_boundaries"] == boundaries
    assert gate["verdict"] in {"PASS", "FAIL", "FRAGILE", "INSUFFICIENT_EVIDENCE"}

    records = _ledger_records(output_dir / "experiments" / "ledger.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["holdout_period"] == summary["holdout_period"]
    assert record["universe_version"].startswith("nifty100-snapshot-")
    assert record["cost_model"] == "india:base"
    assert record["status"] in {"accepted", "rejected"}
    # Rejected experiments are first-class records with a reason.
    if record["status"] == "rejected":
        assert record["reason"]
    assert record["metrics"]["holdout_sharpe"] == pytest.approx(
        summary["holdout_metrics"]["sharpe"]
    )
    assert "pessimistic_holdout_sharpe" in record["metrics"]
    assert record["gate_result"]["verdict"] == summary["research_gate"]["verdict"]

    # Spec 18: explicit warnings/limitations in the machine-readable report,
    # and an unambiguous period regime in the human-readable report.
    assert isinstance(summary["warnings"], list)
    assert len(summary["limitations"]) >= 3
    report = json.loads((output_dir / "momentum_quality.json").read_text())
    assert report["metadata"]["limitations"] == summary["limitations"]
    assert report["metadata"]["holdout_period"] == summary["holdout_period"]
    markdown = (output_dir / "momentum_quality.md").read_text()
    assert "## Periods" in markdown
    assert "backtest_period" in markdown
    assert "holdout_period" in markdown
    assert "distinct evidence regimes" in markdown


def test_cost_scenarios_are_identified_and_ordered(experiment_module, tmp_path) -> None:
    output_dir = tmp_path / "run"
    assert (
        experiment_module.main(["--output-dir", str(output_dir), "--periods", PERIODS])
        == 0
    )
    summary = json.loads(
        (output_dir / "baseline_experiment_summary.json").read_text(encoding="utf-8")
    )
    scenarios = summary["cost_scenario_results"]
    assert set(scenarios) == {"optimistic", "base", "pessimistic"}
    rates = {}
    for name, payload in scenarios.items():
        model = payload["cost_model"]
        assert model["scenario"] == name
        rates[name] = model["table"]["brokerage_bps"]  # table recorded per run
        # Both periods are present and every metric carries its scenario.
        assert set(payload) == {"cost_model", "full_period", "holdout"}
    holdout_returns = {
        name: payload["holdout"]["total_return"] for name, payload in scenarios.items()
    }
    # Same weights, same data: harsher market costs cannot improve results.
    assert holdout_returns["optimistic"] >= holdout_returns["base"]
    assert holdout_returns["base"] >= holdout_returns["pessimistic"]
    assert rates["optimistic"] == rates["base"] == rates["pessimistic"]
