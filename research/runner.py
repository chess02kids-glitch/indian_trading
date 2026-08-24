"""High-level research runner connecting strategies, portfolios, and backtests."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from backtest.benchmarks import benchmark_suite, compare_results
from backtest.engine import BacktestConfig, BacktestResult, VectorBTResearchEngine
from portfolio.construction import EqualWeightConstructor

from .contracts import MarketData, PortfolioConstructor, Strategy
from .universe import Universe


@dataclass(frozen=True, slots=True)
class ResearchRun:
    """Strategy result plus identically-costed benchmark results."""

    result: BacktestResult
    benchmarks: dict[str, BacktestResult]

    def all_results(self) -> dict[str, BacktestResult]:
        """Return the strategy and benchmark results keyed by name."""
        return {self.result.strategy_name: self.result, **self.benchmarks}

    def comparison(self) -> pd.DataFrame:
        """Return the standardized strategy-versus-benchmark metric table."""
        return compare_results(self.all_results())


def run_strategy(
    strategy: Strategy,
    data: MarketData,
    *,
    constructor: PortfolioConstructor | None = None,
    engine: VectorBTResearchEngine | None = None,
    config: BacktestConfig | None = None,
    random_seed: int = 42,
    universe: Universe | None = None,
) -> ResearchRun:
    """Generate signals, construct allocations, run the strategy, and compare benchmarks."""
    research_data = data.select(universe.symbols) if universe is not None else data
    portfolio_constructor = constructor or EqualWeightConstructor()
    backtest_engine = engine or VectorBTResearchEngine(config=config)
    signals = strategy.generate_signals(research_data)
    weights = portfolio_constructor.construct(signals, research_data)
    result = backtest_engine.run(
        research_data.close,
        weights,
        strategy_name=strategy.name,
    )
    benchmarks = benchmark_suite(
        research_data.close,
        weights,
        engine=backtest_engine,
        random_seed=random_seed,
    )
    return ResearchRun(result=result, benchmarks=benchmarks)
