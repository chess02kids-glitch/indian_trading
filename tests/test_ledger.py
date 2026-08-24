"""Phase 2 tests for the hypothesis ledger (HYP-00001 ... sequencing)."""

from __future__ import annotations

from datetime import datetime

from research.contracts import Experiment, ResearchInputError
from research.ledger import (
    HypothesisLedger,
    hypothesis_id,
    parse_hypothesis_number,
)


def test_hypothesis_id_format() -> None:
    assert hypothesis_id(1) == "HYP-00001"
    assert hypothesis_id(42) == "HYP-00042"
    assert parse_hypothesis_number("HYP-00042") == 42


def test_hypothesis_id_rejects_invalid() -> None:
    for bad in (0, -1, True, "x"):
        try:
            hypothesis_id(bad)
        except ResearchInputError:
            pass
        else:
            raise AssertionError(f"hypothesis_id accepted {bad!r}")
    for bad_id in ("HYP-1", "HYP-", "XYP-00001", "hypothesis-00001"):
        try:
            parse_hypothesis_number(bad_id)
        except ResearchInputError:
            pass
        else:
            raise AssertionError(f"bad hypothesis id accepted: {bad_id}")


class TestLedgerSequencing:
    def test_ids_increment(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        first = ledger.record(hypothesis="p1", status="accepted", strategy="momentum")
        second = ledger.record(hypothesis="p2", status="rejected", strategy="x")
        assert first.hypothesis_id == "HYP-00001"
        assert second.hypothesis_id == "HYP-00002"
        assert ledger.next_hypothesis_id() == "HYP-00003"

    def test_sequence_survives_restart(self, tmp_path) -> None:
        path = tmp_path / "ledger.jsonl"
        HypothesisLedger(path).record(hypothesis="p1", status="accepted", strategy="s")
        restarted = HypothesisLedger(path)
        record = restarted.record(hypothesis="p2", status="accepted", strategy="s")
        assert record.hypothesis_id == "HYP-00002"

    def test_rejected_experiments_recorded(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        rejected = ledger.record_rejection(
            "momentum 12m on nifty100 fails out of sample",
            strategy="momentum_12m",
            reason="deflated Sharpe probability below threshold",
            metrics={"sharpe": 0.1},
            dataset_version="synthetic-v1",
        )
        assert rejected.status == "rejected"
        records = ledger.list_records()
        assert len(records) == 1
        assert records[0].reason is not None
        assert records[0].dataset_version == "synthetic-v1"

    def test_explicit_hypothesis_id_preserved(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        record = ledger.record(
            hypothesis_id="HYP-00007",
            hypothesis="chosen",
            status="accepted",
            strategy="s",
        )
        assert record.hypothesis_id == "HYP-00007"
        assert ledger.next_hypothesis_id() == "HYP-00008"

    def test_experiment_outcome_round_trip(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        experiment = Experiment(
            "HYP-00001",
            "momentum",
            {"lookback": 63},
            ["momentum_3m"],
            "nifty100",
            created_at=datetime(2026, 8, 24),
            dataset_version="synthetic-v1",
            cost_model="india:base",
        )
        record = ledger.for_experiment(
            experiment,
            status="accepted",
            hypothesis_text="3m momentum + quality on nifty100",
            metrics={"sharpe": 0.8, "max_drawdown": -0.2},
            dataset_version="synthetic-v1",
            code_commit="abc123",
            backtest_period="2020-01-01/2024-12-31",
            oos_period="2025-01-01/2026-08-24",
            cost_model="india:base",
        )
        assert record.hypothesis_id == "HYP-00001"
        assert record.backtest_period == "2020-01-01/2024-12-31"
        assert ledger.latest() is not None
        assert ledger.latest().oos_period == "2025-01-01/2026-08-24"

    def test_invalid_status_rejected(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        try:
            ledger.record(hypothesis="x", status="meh", strategy="s")
        except ResearchInputError:
            pass
        else:
            raise AssertionError("invalid status accepted")
