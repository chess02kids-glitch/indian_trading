"""Tests for the Strategy Candidate Set (S01-S08) and Research Protocol."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from dashboard.research_api import list_strategies, run_experiment
from research.candidate_set import (
    CANDIDATE_STRATEGY_SPECS,
    CandidateEvaluationResult,
    CandidateSetReport,
    evaluate_candidate_set,
    get_candidate_strategy,
    run_candidate_protocol,
)
from research.contracts import MarketData
from research.factors import (
    DonchianFactor,
    LowVolatilityRankFactor,
    PriceGapFactor,
    RSIFactor,
    extended_factor_set,
)
from research.ledger import HypothesisLedger
from research.strategies import (
    CrossSectionalMomentumStrategy,
    DonchianTrendStrategy,
    GapFadeStrategy,
    LowVolatilityStrategy,
    OrbStrategy,
    PairsTradingStrategy,
    RsiMeanReversionStrategy,
    ValueQualityStrategy,
    strategy_from_name,
)


def _synthetic_market_data(
    n_days: int = 500, n_symbols: int = 8, seed: int = 42
) -> MarketData:
    rng = np.random.RandomState(seed)
    dates = pd.bdate_range(start="2022-01-03", periods=n_days)
    symbols = [f"NSE_{i:02d}" for i in range(n_symbols)]

    returns = rng.normal(0.0004, 0.015, size=(n_days, n_symbols))
    # Give some symbols distinct drift
    returns[:, 0] += 0.001  # Strong winner
    returns[:, 1] -= 0.001  # Laggard

    prices = 100.0 * np.exp(np.cumsum(returns, axis=0))
    close = pd.DataFrame(prices, index=dates, columns=symbols)
    high = close * (1.0 + np.abs(rng.normal(0.005, 0.003, size=close.shape)))
    low = close * (1.0 - np.abs(rng.normal(0.005, 0.003, size=close.shape)))
    volume = pd.DataFrame(
        rng.uniform(10000, 50000, size=close.shape), index=dates, columns=symbols
    )

    return MarketData(close=close, high=high, low=low, volume=volume)


# ---------------------------------------------------------------------------
# Factor Tests
# ---------------------------------------------------------------------------


def test_donchian_factor() -> None:
    data = _synthetic_market_data(100, 4)
    factor = DonchianFactor(window=20)
    res = factor.compute(data)
    assert res.shape == data.close.shape
    # Warmup check
    assert res.iloc[:20].isna().all().all()
    # Post-warmup bounded
    post_warmup = res.iloc[20:].dropna()
    assert not post_warmup.empty


def test_rsi_factor() -> None:
    data = _synthetic_market_data(100, 4)
    factor = RSIFactor(window=14)
    res = factor.compute(data)
    assert res.shape == data.close.shape
    # Values bounded between 0 and 100
    valid = res.dropna()
    assert (valid >= 0.0).all().all()
    assert (valid <= 100.0).all().all()


def test_low_volatility_rank_factor() -> None:
    data = _synthetic_market_data(150, 4)
    factor = LowVolatilityRankFactor(window=63)
    res = factor.compute(data)
    assert res.shape == data.close.shape
    valid = res.iloc[64:].dropna()
    assert (valid >= 0.0).all().all()
    assert (valid <= 1.0).all().all()


def test_price_gap_factor() -> None:
    data = _synthetic_market_data(50, 4)
    factor = PriceGapFactor()
    res = factor.compute(data)
    assert res.shape == data.close.shape
    assert factor.metadata.name == "price_gap"


def test_extended_factor_set() -> None:
    factors = extended_factor_set()
    assert len(factors) == 16
    names = {f.metadata.name for f in factors}
    assert "donchian" in names
    assert "rsi" in names
    assert "low_volatility_rank" in names
    assert "price_gap" in names


# ---------------------------------------------------------------------------
# Individual Candidate Strategy Tests
# ---------------------------------------------------------------------------


def test_s01_cross_sectional_momentum() -> None:
    data = _synthetic_market_data(300, 6)
    strat = CrossSectionalMomentumStrategy(lookback=63, quantile=0.33)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    # Check that top quantile symbols get non-zero signal
    active_per_day = (sig.values > 0).sum(axis=1).iloc[65:]
    assert (active_per_day >= 1).all()


def test_s01_multi_horizon() -> None:
    data = _synthetic_market_data(300, 6)
    strat = CrossSectionalMomentumStrategy(multi_horizon=True, quantile=0.25)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape


def test_s02_donchian_trend() -> None:
    data = _synthetic_market_data(300, 4)
    strat = DonchianTrendStrategy(entry_window=20, exit_window=10)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    assert set(np.unique(sig.values.dropna().to_numpy())).issubset({0.0, 1.0})


def test_s03_pairs_trading_specific_symbols() -> None:
    data = _synthetic_market_data(300, 4)
    strat = PairsTradingStrategy(
        symbol_a="NSE_00",
        symbol_b="NSE_01",
        window=40,
        entry_zscore=1.5,
        exit_zscore=0.5,
    )
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    assert set(sig.values.columns) == set(data.close.columns)


def test_s03_pairs_trading_auto_pairing() -> None:
    data = _synthetic_market_data(300, 4)
    strat = PairsTradingStrategy(window=40, entry_zscore=1.5, exit_zscore=0.5)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape


def test_s04_rsi_mean_reversion() -> None:
    data = _synthetic_market_data(300, 4)
    strat = RsiMeanReversionStrategy(rsi_window=14, oversold=30.0, overbought=70.0)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    assert set(np.unique(sig.values.dropna().to_numpy())).issubset({0.0, 1.0})


def test_s04_rsi_with_trend_filter() -> None:
    data = _synthetic_market_data(300, 4)
    strat = RsiMeanReversionStrategy(
        rsi_window=14, oversold=35.0, overbought=65.0, trend_filter_window=50
    )
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape


def test_s05_orb_strategy() -> None:
    data = _synthetic_market_data(300, 4)
    strat = OrbStrategy(range_factor=0.5, atr_window=14)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    assert set(np.unique(sig.values.dropna().to_numpy())).issubset({0.0, 1.0})


def test_s06_gap_fade_strategy() -> None:
    data = _synthetic_market_data(300, 4)
    strat = GapFadeStrategy(min_gap_pct=-0.005, max_gap_pct=-0.035)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    assert set(np.unique(sig.values.dropna().to_numpy())).issubset({0.0, 1.0})


def test_s07_low_volatility_strategy() -> None:
    data = _synthetic_market_data(300, 6)
    strat = LowVolatilityStrategy(vol_window=63, quantile=0.33)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape
    active_per_day = (sig.values > 0).sum(axis=1).iloc[65:]
    assert (active_per_day >= 1).all()


def test_s08_value_quality_strategy() -> None:
    data = _synthetic_market_data(300, 6)
    strat = ValueQualityStrategy(quality_quantile=0.5, value_quantile=0.5)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape


def test_s08_value_quality_with_fundamentals() -> None:
    data = _synthetic_market_data(300, 3)
    dates = data.close.index[::50]
    rows = []
    for d in dates:
        for sym in ["NSE_00", "NSE_01", "NSE_02"]:
            rows.append(
                {
                    "date": d,
                    "symbol": sym,
                    "roe": 0.15,
                    "debt_to_equity": 0.5,
                    "pe_ratio": 15.0,
                }
            )
    fundamentals = pd.DataFrame(rows)
    strat = ValueQualityStrategy(fundamentals=fundamentals)
    sig = strat.generate_signals(data)
    assert sig.values.shape == data.close.shape


# ---------------------------------------------------------------------------
# Strategy Factory and Alias Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_cls",
    [
        ("s01", CrossSectionalMomentumStrategy),
        ("cross_sectional_momentum", CrossSectionalMomentumStrategy),
        ("s02", DonchianTrendStrategy),
        ("donchian_trend", DonchianTrendStrategy),
        ("turtle", DonchianTrendStrategy),
        ("s03", PairsTradingStrategy),
        ("pairs_trading", PairsTradingStrategy),
        ("stat_arb", PairsTradingStrategy),
        ("s04", RsiMeanReversionStrategy),
        ("rsi_mean_reversion", RsiMeanReversionStrategy),
        ("s05", OrbStrategy),
        ("orb", OrbStrategy),
        ("s06", GapFadeStrategy),
        ("gap_fade", GapFadeStrategy),
        ("s07", LowVolatilityStrategy),
        ("low_volatility", LowVolatilityStrategy),
        ("s08", ValueQualityStrategy),
        ("value_quality", ValueQualityStrategy),
    ],
)
def test_strategy_from_name_all_candidates(name: str, expected_cls: type) -> None:
    instance = strategy_from_name(name)
    assert isinstance(instance, expected_cls)


def test_candidate_strategy_specs_integrity() -> None:
    assert len(CANDIDATE_STRATEGY_SPECS) == 8
    for i, spec in enumerate(CANDIDATE_STRATEGY_SPECS, 1):
        assert spec.candidate_id == f"S{i:02d}"
        assert spec.priority == i
        _, strat = get_candidate_strategy(spec.candidate_id)
        assert strat is not None


# ---------------------------------------------------------------------------
# Candidate Set Evaluation Protocol & Rejection Recording Tests
# ---------------------------------------------------------------------------


def test_run_candidate_protocol_single_strategy(tmp_path: Path) -> None:
    data = _synthetic_market_data(400, 5)
    spec, _ = get_candidate_strategy("S01")
    ledger = HypothesisLedger(tmp_path / "hypothesis_ledger.jsonl")

    eval_result = run_candidate_protocol(
        spec,
        data,
        train_size=200,
        test_size=50,
        placebo_samples=10,
        seed=42,
        ledger=ledger,
    )

    assert isinstance(eval_result, CandidateEvaluationResult)
    assert eval_result.candidate_id == "S01"
    assert eval_result.gate_decision.verdict in (
        "PASS",
        "FRAGILE",
        "FAIL",
        "INSUFFICIENT_EVIDENCE",
    )
    assert "1x" in eval_result.cost_stress_sharpe
    assert "2x" in eval_result.cost_stress_sharpe
    assert "3x" in eval_result.cost_stress_sharpe

    # Verify ledger was written
    records = ledger.list_records()
    assert len(records) == 1
    assert records[0].strategy == "cross_sectional_momentum"


def test_evaluate_candidate_set_end_to_end(tmp_path: Path) -> None:
    data = _synthetic_market_data(400, 5)
    report = evaluate_candidate_set(
        data,
        train_size=200,
        test_size=50,
        placebo_samples=10,
        seed=42,
        ledger_path=tmp_path / "hyp_ledger.jsonl",
        tracking_dir=tmp_path / "experiments",
    )

    assert isinstance(report, CandidateSetReport)
    assert len(report.evaluations) == 8

    # Summary table check
    df = report.to_dataframe()
    assert len(df) == 8
    assert list(df["Candidate"]) == [
        "S01",
        "S02",
        "S03",
        "S04",
        "S05",
        "S06",
        "S07",
        "S08",
    ]

    # Markdown generation check
    md = report.to_markdown()
    assert "Candidate Strategy Set — Protocol Research Report" in md
    assert "S01: Cross-Sectional Momentum" in md
    assert "S05: Opening Range Breakout" in md

    # File output check
    md_path, json_path = report.write(tmp_path)
    assert md_path.is_file()
    assert json_path.is_file()

    with json_path.open() as f:
        payload = json.load(f)
    assert payload["total_candidates"] == 8
    assert len(payload["candidates"]) == 8


# ---------------------------------------------------------------------------
# Dashboard Integration Tests
# ---------------------------------------------------------------------------


def test_dashboard_strategy_catalogue_covers_all_candidates() -> None:
    cat = list_strategies()
    expected_candidates = [
        "cross_sectional_momentum",
        "donchian_trend",
        "pairs_trading",
        "rsi_mean_reversion",
        "orb",
        "gap_fade",
        "low_volatility",
        "value_quality",
    ]
    for key in expected_candidates:
        assert key in cat
        assert "label" in cat[key]
        assert "parameters" in cat[key]


def test_dashboard_run_experiment_with_all_candidates(tmp_path: Path) -> None:
    # Test that each candidate runs via the dashboard research_api endpoint
    for spec in CANDIDATE_STRATEGY_SPECS:
        res = run_experiment(
            spec.name,
            use_synthetic=True,
            train_size=150,
            test_size=40,
            placebo_samples=5,
            seed=42,
            tracking_dir=tmp_path / "dash_exp",
        )
        assert res.strategy == spec.name
        assert res.verdict in ("PASS", "FRAGILE", "FAIL", "INSUFFICIENT_EVIDENCE")
        assert res.metrics is not None
