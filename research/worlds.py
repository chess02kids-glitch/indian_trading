"""Controlled synthetic worlds for framework verification only.

These worlds have a known data-generating process. Passing or failing a
strategy here is evidence about the *research system*, never about Indian
equities. Results must be labelled FRAMEWORK_VERIFICATION.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .contracts import MarketData, ResearchInputError

__all__ = [
    "FRAMEWORK_VERIFICATION",
    "SyntheticWorld",
    "available_worlds",
    "build_world",
    "future_information_present",
]

FRAMEWORK_VERIFICATION = "FRAMEWORK_VERIFICATION"


@dataclass(frozen=True, slots=True)
class SyntheticWorld:
    name: str
    description: str
    data: MarketData
    membership: pd.DataFrame
    fundamentals: pd.DataFrame
    expected: str
    metadata: dict[str, Any]


def _index(periods: int, start: str = "2023-01-02") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=periods, freq="B")


def _symbols(count: int) -> list[str]:
    return [f"S{i:02d}" for i in range(count)]


def _full_membership(index: pd.DatetimeIndex, symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(True, index=index, columns=symbols)


def _empty_fundamentals(index: pd.DatetimeIndex, symbols: list[str]) -> pd.DataFrame:
    rows = []
    for day in index[::63]:
        for symbol in symbols:
            rows.append(
                {
                    "date": day,
                    "symbol": symbol,
                    "roe": 0.15,
                    "debt_to_equity": 0.5,
                }
            )
    return pd.DataFrame(rows)


def world_noise(
    periods: int = 504, n: int = 12, seed: int = 20260824
) -> SyntheticWorld:
    index = _index(periods)
    symbols = _symbols(n)
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, 0.012, size=(periods, n))
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    prices = pd.DataFrame(close, index=index, columns=symbols)
    return SyntheticWorld(
        name="A_noise",
        description="Pure noise. No durable strategy should reliably pass.",
        data=MarketData(close=prices),
        membership=_full_membership(index, symbols),
        fundamentals=_empty_fundamentals(index, symbols),
        expected="No durable strategy should reliably pass.",
        metadata={"kind": FRAMEWORK_VERIFICATION, "signal": None},
    )


def world_momentum(
    periods: int = 504, n: int = 12, seed: int = 20260824
) -> SyntheticWorld:
    index = _index(periods)
    symbols = _symbols(n)
    rng = np.random.default_rng(seed)
    drift = np.linspace(0.0016, -0.0012, n)
    returns = rng.normal(0.0, 0.01, size=(periods, n)) + drift
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    prices = pd.DataFrame(close, index=index, columns=symbols)
    return SyntheticWorld(
        name="B_momentum",
        description="Persistent cross-sectional drift. Momentum should detect it.",
        data=MarketData(close=prices),
        membership=_full_membership(index, symbols),
        fundamentals=_empty_fundamentals(index, symbols),
        expected="Momentum should have a chance to pass relative to noise.",
        metadata={"kind": FRAMEWORK_VERIFICATION, "signal": "momentum"},
    )


def world_mean_reversion(
    periods: int = 504, n: int = 12, seed: int = 20260824
) -> SyntheticWorld:
    index = _index(periods)
    symbols = _symbols(n)
    rng = np.random.default_rng(seed)
    levels = np.zeros((periods, n))
    shock = rng.normal(0.0, 0.02, size=(periods, n))
    for t in range(1, periods):
        levels[t] = 0.85 * levels[t - 1] + shock[t]
    close = 100.0 * np.exp(levels)
    prices = pd.DataFrame(close, index=index, columns=symbols)
    return SyntheticWorld(
        name="C_mean_reversion",
        description="Ornstein-Uhlenbeck-like prices. Reversal should detect it.",
        data=MarketData(close=prices),
        membership=_full_membership(index, symbols),
        fundamentals=_empty_fundamentals(index, symbols),
        expected="Mean-reversion strategy should detect the structure.",
        metadata={"kind": FRAMEWORK_VERIFICATION, "signal": "mean_reversion"},
    )


def world_regime(
    periods: int = 504, n: int = 12, seed: int = 20260824
) -> SyntheticWorld:
    index = _index(periods)
    symbols = _symbols(n)
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0, 0.01, size=(periods, n))
    # First half: momentum drift. Second half: reversal around zero.
    drift = np.linspace(0.0015, -0.001, n)
    returns[: periods // 2] += drift
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    prices = pd.DataFrame(close, index=index, columns=symbols)
    return SyntheticWorld(
        name="D_regime",
        description="Regime switch at the midpoint.",
        data=MarketData(close=prices),
        membership=_full_membership(index, symbols),
        fundamentals=_empty_fundamentals(index, symbols),
        expected="A single static family should be fragile across regimes.",
        metadata={"kind": FRAMEWORK_VERIFICATION, "signal": "regime"},
    )


def world_leakage(
    periods: int = 252, n: int = 8, seed: int = 20260824
) -> SyntheticWorld:
    index = _index(periods)
    symbols = _symbols(n)
    rng = np.random.default_rng(seed)
    future_returns = rng.normal(0.0, 0.015, size=(periods, n))
    close = 100.0 * np.exp(np.cumsum(future_returns, axis=0))
    prices = pd.DataFrame(close, index=index, columns=symbols)
    # Deliberately invalid feature: tomorrow's return available today.
    leak = prices.pct_change().shift(-1)
    return SyntheticWorld(
        name="E_leakage",
        description="Tomorrow's return is attached as a feature. Must be caught.",
        data=MarketData(close=prices),
        membership=_full_membership(index, symbols),
        fundamentals=_empty_fundamentals(index, symbols),
        expected="Leakage test/gate should detect future-information features.",
        metadata={
            "kind": FRAMEWORK_VERIFICATION,
            "signal": "leakage",
            "invalid_feature": leak,
        },
    )


def world_survivorship(
    periods: int = 400, n: int = 10, seed: int = 20260824
) -> SyntheticWorld:
    index = _index(periods)
    symbols = _symbols(n)
    rng = np.random.default_rng(seed)
    drift = np.linspace(0.0012, -0.0018, n)
    returns = rng.normal(0.0, 0.012, size=(periods, n)) + drift
    close = 100.0 * np.exp(np.cumsum(returns, axis=0))
    prices = pd.DataFrame(close, index=index, columns=symbols)
    membership = _full_membership(index, symbols)
    # Delist the worst names in the last third — a survivorship trap if
    # ranking uses the final universe rather than PIT membership.
    cutoff = index[int(periods * 2 / 3)]
    losers = symbols[-3:]
    membership.loc[membership.index >= cutoff, losers] = False
    return SyntheticWorld(
        name="F_survivorship",
        description="Poor names delist late. PIT must prevent look-ahead boost.",
        data=MarketData(close=prices),
        membership=membership,
        fundamentals=_empty_fundamentals(index, symbols),
        expected="PIT ranking must ignore already-delisted names.",
        metadata={"kind": FRAMEWORK_VERIFICATION, "signal": "survivorship"},
    )


def world_multiple_testing(
    periods: int = 300, n: int = 8, seed: int = 20260824
) -> SyntheticWorld:
    return SyntheticWorld(
        name="G_multiple_testing",
        description="Noise world used to generate many random variants.",
        data=world_noise(periods=periods, n=n, seed=seed).data,
        membership=_full_membership(_index(periods), _symbols(n)),
        fundamentals=_empty_fundamentals(_index(periods), _symbols(n)),
        expected="DSR / budget / ledger must prevent naive promotion.",
        metadata={"kind": FRAMEWORK_VERIFICATION, "signal": "multiple_testing"},
    )


_WORLD_BUILDERS = {
    "A": world_noise,
    "A_noise": world_noise,
    "B": world_momentum,
    "B_momentum": world_momentum,
    "C": world_mean_reversion,
    "C_mean_reversion": world_mean_reversion,
    "D": world_regime,
    "D_regime": world_regime,
    "E": world_leakage,
    "E_leakage": world_leakage,
    "F": world_survivorship,
    "F_survivorship": world_survivorship,
    "G": world_multiple_testing,
    "G_multiple_testing": world_multiple_testing,
}


def available_worlds() -> tuple[str, ...]:
    return (
        "A_noise",
        "B_momentum",
        "C_mean_reversion",
        "D_regime",
        "E_leakage",
        "F_survivorship",
        "G_multiple_testing",
    )


def build_world(name: str, **kwargs: Any) -> SyntheticWorld:
    key = name.strip()
    if key not in _WORLD_BUILDERS:
        raise ResearchInputError(f"unknown synthetic world: {name}")
    return _WORLD_BUILDERS[key](**kwargs)


def future_information_present(feature: pd.DataFrame, prices: pd.DataFrame) -> bool:
    """Detect a feature that correlates with *next-day* returns more than lag-0.

    A crude but deterministic leakage screen for framework tests.
    """
    nxt = prices.pct_change().shift(-1)
    aligned = feature.reindex_like(prices)
    current = prices.pct_change()
    # Compare mean abs correlation with t+1 vs t.
    future_corr = aligned.corrwith(nxt).abs().mean()
    now_corr = aligned.corrwith(current).abs().mean()
    if pd.isna(future_corr) or pd.isna(now_corr):
        return False
    return float(future_corr) > float(now_corr) + 0.05
