"""Tests for research contracts, factor families, and strategy interfaces."""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from models.quality import QualityFactor
from research.contracts import (
    CostModel,
    Experiment,
    FactorMetadata,
    MarketData,
    ResearchInputError,
    Signal,
)
from research.factors import (
    ATRFactor,
    BollingerDeviationFactor,
    EMAFactor,
    Momentum1MFactor,
    Momentum3MFactor,
    Momentum6MFactor,
    Momentum12MFactor,
    MomentumFactor,
    MovingAverageCrossoverFactor,
    RelativeStrengthRankFactor,
    RollingVolatilityFactor,
    SMAFactor,
    ZScoreFactor,
    standard_factor_set,
)
from research.strategies import (
    CrossoverStrategy,
    FactorStrategy,
    MeanReversionStrategy,
    MomentumStrategy,
    strategy_from_name,
)
from research.universe import custom_universe, nifty_50, nifty_100, resolve_universe


def _market_data(periods: int = 300) -> MarketData:
    """Build deterministic OHLCV panels with distinct asset behavior."""
    index = pd.date_range("2020-01-01", periods=periods, freq="B")
    close = pd.DataFrame(
        {
            "FAST": np.linspace(100, 220, periods),
            "SLOW": np.linspace(100, 130, periods),
            "MEAN": 100 + np.sin(np.arange(periods) / 5),
        },
        index=index,
    )
    return MarketData(close=close, high=close * 1.02, low=close * 0.98)


def test_market_data_and_contracts_are_aligned_and_serializable() -> None:
    """Core contracts validate alignment and expose reproducible metadata."""
    data = _market_data(20)
    assert data.high is not None
    selected = data.select(["slow"])
    assert selected.close.columns.tolist() == ["SLOW"]
    metadata = FactorMetadata("test", "test", "description", {"window": 2})
    assert metadata.to_dict()["parameters"] == {"window": 2}
    signal = Signal(data.close, {"factor": "test"})
    assert signal.values.equals(data.close)
    costs = CostModel(5, 2)
    assert costs.proportional_rate == pytest.approx(0.0007)
    assert costs.cost(2) == pytest.approx(0.0014)
    experiment = Experiment(
        "H-1",
        "momentum",
        {"lookback": 20},
        ["momentum"],
        "custom",
        created_at=datetime(2024, 1, 1),
    )
    assert len(experiment.experiment_id) == 16
    assert experiment.to_dict()["strategy"] == "momentum"


def test_market_data_from_long_frame() -> None:
    """Canonical long-form data pivots into aligned research panels."""
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-02"]),
            "symbol": ["A", "B", "A", "B"],
            "close": [10, 20, 11, 19],
            "high": [11, 21, 12, 20],
            "low": [9, 19, 10, 18],
        }
    )
    data = MarketData.from_long_frame(frame)
    assert list(data.close.columns) == ["A", "B"]
    assert data.high is not None
    assert data.high.loc[pd.Timestamp("2024-01-02"), "B"] == 20


def test_momentum_factor_families_have_expected_windows() -> None:
    """One, three, six, and twelve-month factors use documented trading windows."""
    data = _market_data()
    factors = [
        Momentum1MFactor(),
        Momentum3MFactor(),
        Momentum6MFactor(),
        Momentum12MFactor(),
    ]
    assert [factor.metadata.name for factor in factors] == [
        "momentum_1m",
        "momentum_3m",
        "momentum_6m",
        "momentum_12m",
    ]
    for factor in factors:
        output = factor.compute(data)
        assert output.index.equals(data.close.index)
        assert output.columns.equals(data.close.columns)
        assert output.iloc[factor.lookback :].notna().any().any()


def test_trend_factors_and_crossover() -> None:
    """SMA, EMA, and crossover factors preserve alignment and warm-up periods."""
    data = _market_data(80)
    sma = SMAFactor(window=5).compute(data)
    ema = EMAFactor(span=5).compute(data)
    crossover = MovingAverageCrossoverFactor(5, 10).compute(data)
    assert sma.iloc[4].notna().all()
    assert ema.iloc[4].notna().all()
    assert crossover.iloc[:9].isna().all().all()
    assert crossover.iloc[-1, 0] == 1


def test_mean_reversion_and_volatility_factors() -> None:
    """Z-score, Bollinger, rolling volatility, ATR, and ranking are reproducible."""
    data = _market_data()
    zscore = ZScoreFactor(20).compute(data)
    bollinger = BollingerDeviationFactor(20).compute(data)
    volatility = RollingVolatilityFactor(20).compute(data)
    atr = ATRFactor(14).compute(data)
    ranks = RelativeStrengthRankFactor(20).compute(data)
    for output in (zscore, bollinger, volatility, atr, ranks):
        assert output.index.equals(data.close.index)
        assert output.columns.equals(data.close.columns)
    assert ranks.iloc[-1]["FAST"] == 1.0
    assert atr.iloc[-1].notna().all()


def test_standard_factor_set_and_strategy_signals() -> None:
    """The standard set and strategy implementations expose factor metadata."""
    data = _market_data()
    factors = standard_factor_set()
    assert len(factors) == 11
    momentum = MomentumStrategy(lookback=20).generate_signals(data)
    factor_strategy = FactorStrategy(MomentumFactor(20), strategy_name="custom_factor")
    assert momentum.values.index.equals(data.close.index)
    assert factor_strategy.generate_signals(data).metadata["strategy"] == "custom_factor"
    assert CrossoverStrategy(5, 10).generate_signals(data).values.shape == data.close.shape
    assert MeanReversionStrategy(20).generate_signals(data).values.shape == data.close.shape


def test_strategy_factory_and_invalid_contract_inputs() -> None:
    """Strategy factory names are stable and invalid inputs fail clearly."""
    assert strategy_from_name("mean-reversion").name == "mean_reversion"
    assert strategy_from_name("momentum", {"lookback": 10}).parameters["lookback"] == 10
    with pytest.raises(ResearchInputError, match="unsupported"):
        strategy_from_name("unknown")
    with pytest.raises(ResearchInputError):
        MomentumFactor(1)
    with pytest.raises(ResearchInputError):
        MovingAverageCrossoverFactor(10, 5)


def test_research_universes_are_configuration_driven() -> None:
    """Built-in snapshots have expected sizes and custom JSON is resolved."""
    assert len(nifty_50().symbols) == 50
    assert len(nifty_100().symbols) == 100
    custom = custom_universe(["abc", "def"], name="my_universe", as_of=date(2024, 1, 1))
    assert custom.contains("ABC")
    assert resolve_universe({"name": "nifty_50"}).name == "nifty50"
    assert resolve_universe({"name": "custom", "symbols": ["A", "B"]}).symbols == ("A", "B")


class _TestQualityFactor(QualityFactor):
    """Concrete test implementation proving the quality interface contract."""

    @property
    def metadata(self) -> FactorMetadata:
        return FactorMetadata("quality_test", "quality", "test quality factor")

    def compute(self, fundamentals: pd.DataFrame) -> pd.DataFrame:
        validated = self.validate_fundamentals(fundamentals)
        return validated[["score"]]


def test_quality_factor_interface_validates_fundamentals() -> None:
    """Quality interfaces accept deterministic fundamentals and reject bad input."""
    factor = _TestQualityFactor()
    fundamentals = pd.DataFrame({"score": [1.0, 2.0]}, index=pd.date_range("2024-01-01", periods=2))
    assert factor.compute(fundamentals).iloc[-1, 0] == 2.0
    with pytest.raises(ResearchInputError):
        factor.compute(pd.DataFrame())
