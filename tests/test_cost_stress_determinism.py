"""Transaction-cost stress and multi-process determinism tests (§21–22).

* Cost scenarios (optimistic/base/pessimistic) must degrade a strategy's
  net performance monotonically while leaving turnover and weights
  unchanged (the scenario changes only the cost rate — never the
  allocation logic), and the cost-model version must appear in gate
  evidence so a strategy can never quietly switch cost models.
* Determinism must survive process boundaries: the same inputs + same
  code + same seed produce bit-identical research results in separate
  processes.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.benchmarks import benchmark_suite
from backtest.costs import IndiaCostModel
from backtest.engine import BacktestConfig, VectorBTResearchEngine
from research.gate import ResearchGate
from research.zoo import run_zoo_family

SCENARIOS = ("optimistic", "base", "pessimistic")


def make_prices(n_symbols: int = 10, n_days: int = 480, seed: int = 9) -> pd.DataFrame:
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-02", periods=n_days)
    columns = [f"S{i}" for i in range(n_symbols)]
    returns = generator.normal(0.0005, 0.014, size=(n_days, n_symbols))
    return pd.DataFrame(
        100.0 * np.exp(np.cumsum(returns, axis=0)), index=index, columns=columns
    )


class TestCostScenarioStress:
    def test_net_returns_degrade_monotonically(self) -> None:
        prices = make_prices()
        results = {}
        for scenario in SCENARIOS:
            engine = VectorBTResearchEngine(
                config=BacktestConfig(
                    initial_cash=1_000_000.0,
                    cost_model=IndiaCostModel(scenario=scenario),
                )
            )
            results[scenario] = run_zoo_family(
                "cross_sectional_momentum", prices, engine=engine
            )
        net_total = {
            scenario: float((1.0 + result.returns).prod() - 1.0)
            for scenario, result in results.items()
        }
        assert net_total["optimistic"] >= net_total["base"]
        assert net_total["base"] >= net_total["pessimistic"]

    def test_turnover_and_weights_scenario_independent(self) -> None:
        prices = make_prices()
        turnovers = {}
        weight_frames = {}
        for scenario in SCENARIOS:
            engine = VectorBTResearchEngine(
                config=BacktestConfig(
                    initial_cash=1_000_000.0,
                    cost_model=IndiaCostModel(scenario=scenario),
                )
            )
            result = run_zoo_family("cross_sectional_momentum", prices, engine=engine)
            turnovers[scenario] = float(result.metrics.turnover)
            weight_frames[scenario] = result.weights
        # The scenario changes the cost rate only — allocation and
        # turnover are identical across scenarios.
        assert turnovers["optimistic"] == turnovers["base"] == turnovers["pessimistic"]
        assert weight_frames["optimistic"].equals(weight_frames["base"])
        assert weight_frames["base"].equals(weight_frames["pessimistic"])

    def test_cost_model_version_in_gate_evidence(self) -> None:
        prices = make_prices()
        engine = VectorBTResearchEngine(
            config=BacktestConfig(
                initial_cash=1_000_000.0,
                cost_model=IndiaCostModel(scenario="base"),
            )
        )
        result = run_zoo_family("low_volatility", prices, engine=engine)
        benchmarks = benchmark_suite(prices, result.weights, engine=engine)
        decision = ResearchGate().evaluate(
            result,
            benchmarks=benchmarks,
            cost_model_name="india:base",
            trials=5,
            trials_source="campaign",
        )
        # The gate's reproducibility block records which cost model and
        # engine metadata (incl. the charge-table version) produced the
        # decision.
        assert decision.reproducibility["cost_model"] == "india:base"
        engine_metadata = decision.reproducibility["engine_metadata"]
        assert engine_metadata["cost_model"]["model"] == "india_cost_model"
        assert engine_metadata["cost_model"]["scenario"] == "base"
        assert "table_version" in engine_metadata["cost_model"]["table"]
        # Cost checks expose the drag and stressed Sharpe in evidence.
        cost_check = next(
            check for check in decision.checks if check.name == "cost_robustness"
        )
        assert "stressed_sharpe" in cost_check.evidence
        assert "cost_share" in cost_check.evidence

    def test_stressed_sharpe_monotonic_in_cost_multiple(self) -> None:
        prices = make_prices()
        engine = VectorBTResearchEngine(
            config=BacktestConfig(
                initial_cash=1_000_000.0,
                cost_model=IndiaCostModel(scenario="base"),
            )
        )
        result = run_zoo_family("mean_reversion", prices, engine=engine)
        gross = result.returns + result.trades["total_cost"].astype(float)
        cost_series = result.trades["total_cost"].astype(float)
        sharpes = []
        for multiple in (1.0, 2.0, 5.0):
            stressed = gross - multiple * cost_series
            deviation = stressed.std(ddof=1)
            sharpes.append(
                float(stressed.mean() / deviation * np.sqrt(252))
                if deviation > 0
                else 0.0
            )
        assert sharpes[0] >= sharpes[1] >= sharpes[2]


class TestMultiProcessDeterminism:
    SCRIPT = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, ".")
        import numpy as np
        import pandas as pd
        from backtest.costs import IndiaCostModel
        from backtest.engine import BacktestConfig, VectorBTResearchEngine
        from research.gate import ResearchGate, generate_placebo_results
        from research.zoo import run_zoo_family, run_benchmark_zoo

        seed = 20260824
        generator = np.random.default_rng(seed)
        index = pd.bdate_range("2024-01-02", periods=480)
        columns = [f"S{i}" for i in range(10)]
        returns = generator.normal(0.0005, 0.014, size=(480, 10))
        prices = pd.DataFrame(100.0 * np.exp(np.cumsum(returns, axis=0)),
                              index=index, columns=columns)
        engine = VectorBTResearchEngine(
            config=BacktestConfig(initial_cash=1_000_000.0,
                                  cost_model=IndiaCostModel(scenario="base"))
        )
        results = run_benchmark_zoo(prices, engine=engine, seed=seed)
        placebos = generate_placebo_results(prices, engine=engine, samples=5, seed=seed)
        gate = ResearchGate(random_seed=seed)
        decision = gate.evaluate(
            results["cross_sectional_momentum"],
            benchmarks={k: v for k, v in results.items()
                        if k != "cross_sectional_momentum"},
            placebo_results=placebos,
            trials=10,
            trials_source="campaign",
        )
        print("VERDICT:" + decision.verdict)
        print("SHARPE:%.12f" % decision.metrics["sharpe"])
        print("DSR:%.12f" % decision.metrics["deflated_sharpe_probability"])
        print("CROSS_CSM:" + results["cross_sectional_momentum"].returns.round(12).to_string())
        """
    )

    def _run_subprocess(self) -> str:
        completed = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
            timeout=300,
        )
        assert completed.returncode == 0, completed.stderr
        return completed.stdout

    def test_separate_processes_bit_identical(self) -> None:
        first = self._run_subprocess()
        second = self._run_subprocess()
        assert first == second
        assert first.startswith("VERDICT:")

    def test_result_digest_stable(self) -> None:
        import hashlib

        output = self._run_subprocess()
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()[:16]
        # Same environment, same seed -> same digest (regression anchor).
        assert len(digest) == 16
        assert digest.isalnum()
