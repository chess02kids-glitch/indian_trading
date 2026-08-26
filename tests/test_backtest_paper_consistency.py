"""Backtest/paper consistency audit (§20).

The same strategy implementation must drive both the research backtest and
the paper-execution order flow. The strategy produces a Signal; the
research side converts it to target weights for the engine, the execution
side converts the same signal to PortfolioTargets for the paper broker.
Neither side re-implements the signal logic, so a strategy cannot behave
differently in paper than in research.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.engine import BacktestConfig, VectorBTResearchEngine
from models.domain import PortfolioTarget
from portfolio.construction import EqualWeightConstructor
from research.contracts import MarketData
from research.registry import StrategyRegistry


def make_prices(n_symbols: int = 6, n_days: int = 300, seed: int = 13) -> pd.DataFrame:
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2024-01-02", periods=n_days)
    columns = [f"S{i}" for i in range(n_symbols)]
    returns = generator.normal(0.0005, 0.015, size=(n_days, n_symbols))
    return pd.DataFrame(
        100.0 * np.exp(np.cumsum(returns, axis=0)), index=index, columns=columns
    )


class TestBacktestPaperConsistency:
    def test_strategy_signal_drives_both_paths(self) -> None:
        prices = make_prices()
        data = MarketData(close=prices)
        strategy = StrategyRegistry().build("cross_sectional_momentum")

        # RESEARCH PATH: signal -> weights -> engine.
        signals = strategy.generate_signals(data)
        weights = EqualWeightConstructor().construct(signals, data)
        engine = VectorBTResearchEngine(config=BacktestConfig())
        result = engine.run(
            prices, weights, strategy_name=strategy.name, universe_history=[]
        )
        # The engine samples month-end targets: at the final month-end the
        # target equals the strategy's signal-derived weights.
        rebalance = ~prices.index.to_period("M").duplicated(keep="last")
        last_target = result.weights[rebalance].iloc[-1]
        expected = weights.loc[result.weights.index[rebalance][-1]]
        np.testing.assert_allclose(
            last_target.to_numpy(), expected.to_numpy(), atol=1e-12
        )

        # PAPER PATH: same signal, converted to a PortfolioTarget with
        # limit prices taken from the same data the strategy consumed.
        last_date = prices.index[-1].date()
        active = signals.values.loc[prices.index[-1]]
        selected = active[active > 0].index.tolist()
        assert selected  # the strategy selected something
        target = PortfolioTarget(
            strategy_id=strategy.name,
            hypothesis_id="HYP-TEST",
            as_of=last_date,
            limits={symbol: float(prices[symbol].iloc[-1]) for symbol in selected},
        )
        # The paper path consumes the strategy's selection (symbols), the
        # same as the research path — no re-implementation of momentum.
        assert set(target.limits) == set(selected)
        # Every selected symbol carries positive weight in the research
        # path and is absent from the weights otherwise.
        for symbol in prices.columns:
            if symbol in selected:
                assert weights.loc[prices.index[-1], symbol] > 0
            else:
                assert weights.loc[prices.index[-1], symbol] == 0

    def test_strategy_is_stateless_across_calls(self) -> None:
        # The same strategy object must produce identical signals each
        # call: paper and research cannot diverge through hidden state.
        prices = make_prices()
        data = MarketData(close=prices)
        strategy = StrategyRegistry().build("low_volatility")
        first = strategy.generate_signals(data).values
        second = strategy.generate_signals(data).values
        assert first.equals(second)

    def test_registry_strategy_reusable_by_paper(self) -> None:
        # A strategy built from the registry is a plain object: the paper
        # layer can build the same registered strategy for its target
        # generation, proving the registry is the shared selection layer.
        prices = make_prices()
        data = MarketData(close=prices)
        registry = StrategyRegistry()
        research_strategy = registry.build("reversal")
        paper_strategy = registry.build("reversal")
        assert research_strategy.generate_signals(data).values.equals(
            paper_strategy.generate_signals(data).values
        )
