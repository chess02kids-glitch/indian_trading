"""Tests for the automated research gate (PASS / FAIL / FRAGILE /
INSUFFICIENT_EVIDENCE) and the seeded placebo family."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.benchmarks import benchmark_suite
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from backtest.validation import run_walk_forward
from portfolio.construction import EqualWeightConstructor
from research.contracts import (
    CostModel,
    MarketData,
    ResearchInputError,
    Signal,
    Strategy,
)
from research.gate import (
    GateCheck,
    GateVerdict,
    ResearchGate,
    ResearchGateConfig,
    generate_placebo_results,
)

_NAMES = ["A", "B", "C", "D"]


def _data(periods: int = 520, *, weak: bool = False) -> MarketData:
    """Synthetic market where A drifts strongly and the rest are flat."""
    index = pd.date_range("2020-01-01", periods=periods, freq="B")
    rng = np.random.default_rng(19)
    columns = []
    for position, name in enumerate(_NAMES):
        drift = (
            -0.0001 if (weak and position > 0) else (0.0025 if position == 0 else 0.0)
        )
        noise = 0.008 if position == 0 else 0.008
        values = drift + rng.normal(0, noise, periods)
        columns.append(100 * np.exp(np.cumsum(values)))
    return MarketData(
        pd.DataFrame(np.column_stack(columns), index=index, columns=_NAMES)
    )


class _MomentumWinner(Strategy):
    """Holds only the best-drifting asset after a warm-up window."""

    @property
    def name(self) -> str:
        return "winner"

    def generate_signals(self, data: MarketData) -> Signal:
        values = pd.DataFrame(0.0, index=data.close.index, columns=data.close.columns)
        values.iloc[21:] = 1.0
        values[_NAMES[1:]] = 0.0
        return Signal(values)


class _FlatStrategy(Strategy):
    """Zero-signal strategy (always in cash) used for FAIL scenarios."""

    @property
    def name(self) -> str:
        return "flat"

    def generate_signals(self, data: MarketData) -> Signal:
        return Signal(
            pd.DataFrame(0.0, index=data.close.index, columns=data.close.columns)
        )


def _engine() -> VectorBTResearchEngine:
    return VectorBTResearchEngine(
        BacktestConfig(use_vectorbt=False, cost_model=CostModel(0, 0))
    )


def _run(strategy: Strategy, data: MarketData, *, validate: bool = True) -> tuple:
    engine = _engine()
    weights = EqualWeightConstructor().construct(strategy.generate_signals(data), data)
    result = engine.run(
        data.close, weights, strategy_name=strategy.name, universe_history=[]
    )
    benchmarks = benchmark_suite(data.close, weights, engine=engine, random_seed=42)
    validated = None
    if validate:
        validated = run_walk_forward(
            strategy,
            data,
            EqualWeightConstructor(),
            engine,
            train_size=126,
            test_size=63,
            purge=5,
            embargo=2,
        )
    return result, benchmarks, validated, weights


def _gate(**config) -> ResearchGate:
    return ResearchGate(
        ResearchGateConfig(**config) if config else None,
        random_seed=42,
        git_commit="abc123",
        dataset_fingerprint="ds-fpr",
    )


class TestGateVerdscts:
    def test_strong_strategy_passes(self) -> None:
        """A strategy that beats all benchmarks and placebos passes."""
        data = _data(periods=520)
        result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
        placebos = generate_placebo_results(
            data.close, engine=_engine(), samples=30, seed=42
        )
        decision = _gate().evaluate(
            result,
            benchmarks=benchmarks,
            validation=validated,
            placebo_results=placebos,
            universe="synthetic",
        )
        assert decision.verdict == GateVerdict.PASS.value
        assert decision.score >= 90
        assert decision.failures == ()
        assert all(check.status != "fail" for check in decision.checks), [
            check.to_dict() for check in decision.checks
        ]
        assert "PASS" in decision.to_markdown()
        # Every decision explains itself.
        assert len(decision.checks) == 8
        assert all(check.message for check in decision.checks)

    def test_zero_signals_fail_with_reasons(self) -> None:
        """A cash strategy cannot pass: it fails statistical confidence."""
        data = _data()
        result, benchmarks, validated, _ = _run(_FlatStrategy(), data)
        decision = _gate().evaluate(
            result,
            benchmarks=benchmarks,
            validation=validated,
        )
        assert decision.verdict == GateVerdict.FAIL.value
        assert decision.failures
        assert any(
            check.name == "statistical_confidence" for check in decision.failures
        )
        assert any("Sharpe" in check.message for check in decision.checks)

    def test_missing_validation_gives_fragile_not_pass(self) -> None:
        """No validation evidence can never be silently approved."""
        data = _data(periods=520)
        result, benchmarks, _, _ = _run(_MomentumWinner(), data)
        decision = _gate().evaluate(result, benchmarks=benchmarks)
        assert decision.verdict == GateVerdict.FRAGILE.value
        assert any(
            check.name == "validation_consistency" and check.status == "warn"
            for check in decision.checks
        )

    def test_missing_placebos_are_flagged_not_silent(self) -> None:
        data = _data(periods=520)
        result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
        decision = _gate().evaluate(
            result,
            benchmarks=benchmarks,
            validation=validated,
            placebo_results={},
        )
        assert decision.verdict != GateVerdict.PASS.value
        assert any(
            check.name == "placebo_dominance" and check.status == "warn"
            for check in decision.checks
        )

    def test_insufficient_evidence_short_fails_closed(self) -> None:
        data = _data(periods=120)
        result, benchmarks, _, _ = _run(_MomentumWinner(), data, validate=False)
        decision = _gate().evaluate(result, benchmarks=benchmarks)
        assert decision.verdict == GateVerdict.INSUFFICIENT_EVIDENCE.value
        assert decision.score == 0.0
        assert any(
            check.name == "evidence_sufficiency" and check.status == "fail"
            for check in decision.checks
        )

    def test_drawdown_and_turnover_limits_are_enforced(self) -> None:
        data = _data()
        result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
        gate = _gate(max_drawdown_limit=-0.01, max_turnover_multiple=0.0)
        decision = gate.evaluate(
            result,
            benchmarks=benchmarks,
            validation=validated,
        )
        assert decision.verdict == GateVerdict.FAIL.value
        names = {check.name for check in decision.failures}
        assert {"drawdown_control", "turnover_control"} <= names

    def test_gate_records_reproducibility_metadata(self) -> None:
        data = _data(periods=520)
        result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
        decision = _gate().evaluate(
            result,
            benchmarks=benchmarks,
            validation=validated,
            universe="synthetic",
            strategy_version="2.1",
            factor_versions={"momentum_3m": "1.0"},
            cost_model_name="india:base",
            rebalance_frequency="M",
            validation_method="walk_forward",
            generated_at=pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime(),
        )
        reproducibility = decision.reproducibility
        assert reproducibility["strategy_version"] == "2.1"
        assert reproducibility["factor_versions"] == {"momentum_3m": "1.0"}
        assert reproducibility["git_commit"] == "abc123"
        assert reproducibility["dataset_fingerprint"] == "ds-fpr"
        assert reproducibility["cost_model"] == "india:base"
        assert (
            decision.generated_at
            == pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
        )

    def test_decision_is_deterministic(self) -> None:
        data = _data(periods=520)
        result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
        stamp = pd.Timestamp("2024-01-01T00:00:00Z").to_pydatetime()
        first = _gate().evaluate(
            result, benchmarks=benchmarks, validation=validated, generated_at=stamp
        )
        second = _gate().evaluate(
            result, benchmarks=benchmarks, validation=validated, generated_at=stamp
        )
        assert first.to_dict() == second.to_dict()

    def test_confidence_intervals_and_benchmarks_recorded(self) -> None:
        data = _data(periods=520)
        result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
        decision = _gate().evaluate(result, benchmarks=benchmarks, validation=validated)
        assert {"sharpe", "cagr", "max_drawdown", "volatility"} <= set(
            decision.confidence_intervals
        )
        assert {
            "buy_and_hold",
            "equal_weight",
            "inverse_volatility",
            "persistence",
            "random",
        } <= set(decision.benchmarks)


class TestGateValidation:
    def test_gate_check_rejects_bad_status(self) -> None:
        with pytest.raises(ResearchInputError):
            GateCheck(name="x", status="maybe", message="y")

    def test_config_rejects_impossible_thresholds(self) -> None:
        with pytest.raises(ResearchInputError):
            ResearchGateConfig(max_drawdown_limit=0.0)
        with pytest.raises(ResearchInputError):
            ResearchGateConfig(dsr_min_probability=1.5)
        with pytest.raises(ResearchInputError):
            ResearchGateConfig(minimum_observations=1)


class TestPlaceboFamily:
    def test_placebos_are_seed_deterministic(self) -> None:
        data = _data(periods=120)
        engine = _engine()
        first = generate_placebo_results(data.close, engine=engine, samples=8, seed=7)
        second = generate_placebo_results(data.close, engine=engine, samples=8, seed=7)
        other = generate_placebo_results(data.close, engine=engine, samples=8, seed=8)
        assert set(first) == {f"placebo_{number:05d}" for number in range(8)}
        assert {name: result.metrics.to_dict() for name, result in first.items()} == {
            name: result.metrics.to_dict() for name, result in second.items()
        }
        assert {name: result.metrics.to_dict() for name, result in first.items()} != {
            name: result.metrics.to_dict() for name, result in other.items()
        }

    def test_placebo_rejects_invalid_samples(self) -> None:
        data = _data(periods=120)
        with pytest.raises(ResearchInputError):
            generate_placebo_results(data.close, engine=_engine(), samples=0)


def test_gate_write_produces_json(tmp_path) -> None:
    data = _data(periods=520)
    result, benchmarks, validated, _ = _run(_MomentumWinner(), data)
    decision = _gate().evaluate(result, benchmarks=benchmarks, validation=validated)
    path = decision.write(tmp_path)
    assert path.is_file()
    payload = __import__("json").loads(path.read_text())
    assert payload["verdict"] == decision.verdict
    assert all("message" in check for check in payload["checks"])
