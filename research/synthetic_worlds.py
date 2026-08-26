"""Controlled synthetic worlds for framework verification (worlds A–G).

These worlds test whether the research framework behaves correctly when the
truth is KNOWN. Each world injects a controlled structure (or no structure)
into a deterministic return process, and the zoo/gate/ledger machinery is
run against it. A framework result that contradicts the injected truth is a
framework bug; a result that matches it is framework verification only.

Synthetic results are NEVER evidence about real Indian equities. They are
calibration, not alpha.

Worlds:

* A — pure noise: no structure; no strategy should reliably pass the gate.
* B — momentum: AR(1) positive return autocorrelation; momentum families
  should have a chance to pass.
* C — mean reversion: prices revert to their 20-day mean; reversal family
  should detect the structure.
* D — regime switching: hidden two-state Markov market regime; trend
  following should beat naive passive portfolios.
* E — leakage trap: a deliberately future-information feature ("tomorrow's
  return") that the lookahead audit must flag.
* F — survivorship trap: poor performers are delisted mid-history; the PIT
  membership panel must prevent the artificial performance boost.
* G — multiple-testing trap: pure noise plus an unbounded variant factory;
  budget + DSR controls must prevent naive promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from .contracts import MarketData, ResearchInputError

__all__ = [
    "SyntheticWorld",
    "WORLDS",
    "build_world",
    "leak_feature_for",
    "world_a_noise",
    "world_b_momentum",
    "world_c_mean_reversion",
    "world_d_regime",
    "world_e_leakage",
    "world_f_survivorship",
    "world_g_multiple_testing",
]

#: Default world dimensions (large enough for 252-day holdouts).
DEFAULT_SYMBOLS = 40
DEFAULT_DAYS = 1100


def _calendar(n_days: int) -> pd.DatetimeIndex:
    return pd.bdate_range("2022-01-03", periods=n_days)


def _panel(
    returns: np.ndarray,
    index: pd.DatetimeIndex,
    columns: list[str],
    start_price: float = 100.0,
) -> pd.DataFrame:
    prices = start_price * np.exp(np.cumsum(returns, axis=0))
    return pd.DataFrame(prices, index=index, columns=columns)


def _fundamentals_for(
    index: pd.DatetimeIndex, columns: list[str], seed: int
) -> pd.DataFrame:
    """Quarterly point-in-time fundamentals (random, stable per seed)."""
    generator = np.random.default_rng(seed)
    rows = []
    for date in pd.date_range(index[0], index[-1], freq="QE"):
        for symbol in columns:
            rows.append(
                {
                    "date": date,
                    "symbol": symbol,
                    "roe": 0.05 + 0.05 * generator.random(),
                    "debt_to_equity": 0.2 + 0.4 * generator.random(),
                }
            )
    return pd.DataFrame(rows)


def _panel_fingerprint(close: pd.DataFrame) -> str:
    """Deterministic fingerprint of a close panel (index + columns + values)."""
    import hashlib

    values = np.round(close.to_numpy(dtype=float), 12)
    digest = hashlib.sha256(values.tobytes()).hexdigest()[:24]
    identity = (
        "|".join(close.index.strftime("%Y-%m-%d"))
        + "|"
        + "|".join(str(column) for column in close.columns)
    )
    identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{identity_digest}{digest[:8]}"


@dataclass(frozen=True, slots=True)
class SyntheticWorld:
    """One controlled world: market data plus the injected truth."""

    world_id: str
    name: str
    description: str
    truth: Mapping[str, Any]
    market_data: MarketData
    seed: int
    membership: pd.DataFrame | None = None
    fundamentals: pd.DataFrame | None = None
    expected_families: tuple[str, ...] = ()
    leak_features: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """Deterministic fingerprint of the world's price panel."""
        return _panel_fingerprint(self.market_data.close)


def world_a_noise(
    *,
    n_symbols: int = DEFAULT_SYMBOLS,
    n_days: int = DEFAULT_DAYS,
    seed: int = 20260824,
) -> SyntheticWorld:
    """World A: independent Gaussian returns, no structure."""
    generator = np.random.default_rng(seed)
    index = _calendar(n_days)
    columns = [f"NOISE{i:02d}" for i in range(n_symbols)]
    returns = generator.normal(0.0, 0.015, size=(n_days, n_symbols))
    prices = _panel(returns, index, columns)
    return SyntheticWorld(
        world_id="A",
        name="pure_noise",
        description=(
            "independent Gaussian daily returns; no time-series or "
            "cross-sectional structure"
        ),
        truth={
            "signal": "none",
            "return_process": "iid normal(0, 0.015^2)",
            "expected_outcome": "no strategy reliably passes the gate",
        },
        market_data=MarketData(close=prices),
        seed=seed,
        fundamentals=_fundamentals_for(index, columns, seed),
        expected_families=(),
    )


def world_b_momentum(
    *,
    n_symbols: int = DEFAULT_SYMBOLS,
    n_days: int = DEFAULT_DAYS,
    seed: int = 20260824,
    drift_spread: float = 0.0015,
    volatility: float = 0.015,
) -> SyntheticWorld:
    """World B: persistent cross-sectional drift (momentum).

    Each symbol draws a permanent drift ``m_i ~ N(0, drift_spread)`` once;
    daily returns are ``r_t = m_i + epsilon``. Trailing returns therefore
    rank symbols by their latent drift, so cross-sectional momentum (rank
    past 126-day returns, hold the top quartile) has a genuine, durable
    edge — the cleanest possible momentum structure.
    """
    if not 0 < drift_spread < 0.01:
        raise ResearchInputError("drift_spread must be within (0, 0.01)")
    generator = np.random.default_rng(seed)
    index = _calendar(n_days)
    columns = [f"MOM{i:02d}" for i in range(n_symbols)]
    drift = generator.normal(0.0, drift_spread, size=n_symbols)
    shocks = generator.normal(0.0, volatility, size=(n_days, n_symbols))
    returns = drift[None, :] + shocks
    prices = _panel(returns, index, columns)
    return SyntheticWorld(
        world_id="B",
        name="momentum",
        description=(
            "per-symbol permanent drift m_i ~ N(0, "
            f"{drift_spread}); returns r_t = m_i + epsilon — trailing "
            "returns rank symbols by latent drift"
        ),
        truth={
            "signal": "momentum",
            "return_process": f"persistent_drift(sigma_m={drift_spread}, "
            f"sigma_eps={volatility})",
            "latent_drifts": [float(value) for value in drift],
            "expected_families": [
                "cross_sectional_momentum",
                "persistence",
            ],
        },
        market_data=MarketData(close=prices),
        seed=seed,
        fundamentals=_fundamentals_for(index, columns, seed),
        expected_families=(
            "cross_sectional_momentum",
            "persistence",
        ),
    )


def world_c_mean_reversion(
    *,
    n_symbols: int = DEFAULT_SYMBOLS,
    n_days: int = DEFAULT_DAYS,
    seed: int = 20260824,
    kappa: float = 0.08,
    volatility: float = 0.015,
) -> SyntheticWorld:
    """World C: prices revert to their 20-day moving average.

    The daily log-return pulls the price back toward its trailing mean:
    ``r_t = -kappa * z_{t-1} + epsilon`` where ``z`` is the 20-day log
    deviation. Oversold assets bounce.
    """
    if not 0 < kappa < 0.5:
        raise ResearchInputError("kappa must be within (0, 0.5)")
    generator = np.random.default_rng(seed)
    index = _calendar(n_days)
    columns = [f"REV{i:02d}" for i in range(n_symbols)]
    log_prices = np.zeros((n_days, n_symbols))
    shocks = generator.normal(0.0, volatility, size=(n_days, n_symbols))
    for day in range(1, n_days):
        window = log_prices[max(0, day - 20) : day]
        mean = window.mean(axis=0) if len(window) else log_prices[day - 1]
        deviation = log_prices[day - 1] - mean
        log_prices[day] = log_prices[day - 1] - kappa * deviation + shocks[day]
    prices = pd.DataFrame(np.exp(log_prices) * 100.0, index=index, columns=columns)
    return SyntheticWorld(
        world_id="C",
        name="mean_reversion",
        description=(f"log prices pulled toward their 20-day mean with kappa={kappa}"),
        truth={
            "signal": "mean_reversion",
            "return_process": f"revert(kappa={kappa}, window=20)",
            "expected_families": ["mean_reversion"],
        },
        market_data=MarketData(close=prices),
        seed=seed,
        fundamentals=_fundamentals_for(index, columns, seed),
        expected_families=("mean_reversion",),
    )


def world_d_regime(
    *,
    n_symbols: int = DEFAULT_SYMBOLS,
    n_days: int = DEFAULT_DAYS,
    seed: int = 20260824,
    good_mu: float = 0.0015,
    bad_mu: float = -0.0030,
    good_sigma: float = 0.007,
    bad_sigma: float = 0.020,
    schedule: tuple[tuple[int, int], ...] = ((0, 0), (550, 1), (900, 0)),
    market_beta: float = 0.9,
    idiosyncratic_sigma: float = 0.003,
) -> SyntheticWorld:
    """World D: deterministic two-regime market schedule.

    All symbols share a market factor whose drift/volatility follow a
    *deterministic regime schedule* (``(start_day, regime)`` pairs): the
    strategy only sees prices, so the schedule is hidden from it even
    though the world generator is fully deterministic. The default
    schedule is a ~2-year bull market, a ~14-month bear market (negative
    drift, elevated volatility), then a bull recovery — long enough for a
    200-day trend filter to confirm the regime change and exit before the
    bear's damage. Trend following should beat naive passive portfolios
    on this world.
    """
    if not 0 < market_beta <= 1:
        raise ResearchInputError("market_beta must be in (0, 1]")
    if not schedule or schedule[0][0] != 0:
        raise ResearchInputError("schedule must start at day 0")
    generator = np.random.default_rng(seed)
    index = _calendar(n_days)
    columns = [f"REG{i:02d}" for i in range(n_symbols)]
    regime = np.zeros(n_days, dtype=int)
    for start_day, state in schedule:
        if start_day >= n_days:
            continue
        regime[start_day:] = int(state)
    mus = np.where(regime == 0, good_mu, bad_mu)
    sigmas = np.where(regime == 0, good_sigma, bad_sigma)
    market = generator.normal(mus, sigmas)
    idiosyncratic = generator.normal(0.0, idiosyncratic_sigma, size=(n_days, n_symbols))
    returns = market[:, None] * market_beta + idiosyncratic
    prices = _panel(returns, index, columns)
    return SyntheticWorld(
        world_id="D",
        name="regime_switching",
        description=(
            "deterministic two-regime market schedule "
            f"{schedule}; regime 0 = low-vol positive drift, regime 1 = "
            "high-vol negative drift (hidden from the strategy — only "
            f"prices are visible; market_beta={market_beta})"
        ),
        truth={
            "signal": "regime",
            "regimes": {
                "good": {"mu": good_mu, "sigma": good_sigma},
                "bad": {"mu": bad_mu, "sigma": bad_sigma},
            },
            "regime_schedule": list(schedule),
            "regime_series": list(map(int, regime)),
            "expected_families": ["trend_following"],
        },
        market_data=MarketData(close=prices),
        seed=seed,
        fundamentals=_fundamentals_for(index, columns, seed),
        expected_families=("trend_following",),
    )


def world_e_leakage(
    *,
    n_symbols: int = DEFAULT_SYMBOLS,
    n_days: int = DEFAULT_DAYS,
    seed: int = 20260824,
) -> SyntheticWorld:
    """World E: leakage trap — a perfect future-information feature.

    The world's truth documents a ``leak`` feature equal to the *next-day*
    return (see :func:`leak_feature_for`): an unbeatable signal that must
    be flagged by the lookahead audit. The standard strategy contract
    cannot even see the leak (it is not part of MarketData); the audit
    recomputes candidate factors on truncated histories and must detect
    the difference.
    """
    generator = np.random.default_rng(seed)
    index = _calendar(n_days)
    columns = [f"LEAK{i:02d}" for i in range(n_symbols)]
    returns = generator.normal(0.0, 0.015, size=(n_days, n_symbols))
    prices = _panel(returns, index, columns)
    return SyntheticWorld(
        world_id="E",
        name="leakage_trap",
        description=(
            "noise prices plus a declared leak feature equal to the next "
            "day's return; the leak must be detected, never used"
        ),
        truth={
            "signal": "none",
            "leak_feature": "next_day_return (perfect foresight)",
            "detection": "lookahead audit must flag any factor using it",
        },
        market_data=MarketData(close=prices),
        seed=seed,
        fundamentals=_fundamentals_for(index, columns, seed),
        leak_features=("next_day_return",),
    )


def leak_feature_for(world: SyntheticWorld) -> pd.DataFrame:
    """Materialise world E's leak feature: tomorrow's return.

    Deterministic from the world's close panel: ``leak[t] = r[t+1]`` with
    the final row zero-padded. Any factor computed from this panel is
    using future information by construction.
    """
    if world.world_id != "E":
        raise ResearchInputError("leak_feature_for is only defined for world E")
    close = world.market_data.close
    next_returns = close.shift(-1).div(close) - 1.0
    return next_returns.fillna(0.0)


def world_f_survivorship(
    *,
    n_symbols: int = DEFAULT_SYMBOLS,
    n_days: int = DEFAULT_DAYS,
    seed: int = 20260824,
    n_doomed: int = 10,
) -> SyntheticWorld:
    """World F: survivorship trap — poor performers delist mid-history.

    ``n_doomed`` symbols have negative drift and are removed from the
    index at staggered dates. The final membership panel is point-in-time:
    each doomed symbol is a member only until its delist date. The raw
    price panel keeps full histories so the PIT handling (and its absence)
    is directly measurable.
    """
    if not 0 < n_doomed < n_symbols:
        raise ResearchInputError("n_doomed must be between 1 and n_symbols - 1")
    generator = np.random.default_rng(seed)
    index = _calendar(n_days)
    columns = [f"SURV{i:02d}" for i in range(n_symbols)]
    drift = np.zeros(n_symbols)
    drift[:n_doomed] = -0.0012  # doomed symbols bleed
    shocks = generator.normal(0.0, 0.018, size=(n_days, n_symbols))
    returns = drift[None, :] + shocks
    prices = _panel(returns, index, columns)

    # Staggered delist dates: doomed i delists around day 350 + i * 40.
    membership = pd.DataFrame(True, index=index, columns=columns)
    delist_dates: dict[str, str] = {}
    for i in range(n_doomed):
        delist_index = min(n_days - 1, 350 + i * 40)
        delist_date = index[delist_index]
        membership.loc[membership.index >= delist_date, columns[i]] = False
        delist_dates[columns[i]] = delist_date.isoformat()
    return SyntheticWorld(
        world_id="F",
        name="survivorship",
        description=(
            f"{n_doomed} negative-drift symbols delist at staggered dates; "
            "the point-in-time membership panel must prevent the delisting "
            "boost"
        ),
        truth={
            "signal": "none",
            "doomed_symbols": list(columns[:n_doomed]),
            "delist_dates": delist_dates,
            "expected_outcome": (
                "PIT membership removes the survivorship boost; naive "
                "full-universe backtests show an artificial gain"
            ),
        },
        market_data=MarketData(close=prices),
        seed=seed,
        membership=membership,
        fundamentals=_fundamentals_for(index, columns, seed),
        expected_families=(),
    )


def world_g_multiple_testing(
    *,
    n_symbols: int = 24,
    n_days: int = 700,
    seed: int = 20260824,
) -> SyntheticWorld:
    """World G: multiple-testing trap — noise plus an unbounded factory.

    Provides a documented ``variant_factory`` that keeps producing random
    parameter variants of the momentum family. On pure noise, enough
    variants will eventually look good by luck; campaign budget + DSR
    controls must prevent promotion.
    """
    world = world_a_noise(n_symbols=n_symbols, n_days=n_days, seed=seed)
    world = SyntheticWorld(
        world_id="G",
        name="multiple_testing",
        description=(
            "pure noise with an unbounded random-variant factory; the "
            "research budget and DSR must bound the search"
        ),
        truth={
            "signal": "none",
            "expected_outcome": (
                "RESEARCH_BUDGET_EXHAUSTED before unbounded search; no "
                "lucky variant promoted"
            ),
        },
        market_data=world.market_data,
        seed=seed,
        fundamentals=world.fundamentals,
        expected_families=(),
    )
    return world


def _random_momentum_variant(
    generator: np.random.default_rng,
) -> dict[str, Any]:
    """One random momentum-family parameter variant (world G factory).

    Samples are drawn inside the strategy registry's declared bounds for
    ``cross_sectional_momentum``: the point of world G is that even
    registry-valid parameter search must be bounded by the campaign
    budget — not that invalid parameters are rejected (that is the
    registry's job, tested elsewhere).
    """
    return {
        "lookback": int(generator.integers(42, 250)),
        "quantile": float(generator.uniform(0.05, 0.5)),
    }


def variant_factory(world: SyntheticWorld, seed: int) -> Callable[[], dict[str, Any]]:
    """Return a deterministic unbounded variant factory for world G."""
    if world.world_id != "G":
        raise ResearchInputError("variant_factory is only defined for world G")
    generator = np.random.default_rng(seed)

    def _next() -> dict[str, Any]:
        return _random_momentum_variant(generator)

    return _next


#: Registered worlds: world_id -> builder.
WORLDS: dict[str, Callable[..., SyntheticWorld]] = {
    "A": world_a_noise,
    "B": world_b_momentum,
    "C": world_c_mean_reversion,
    "D": world_d_regime,
    "E": world_e_leakage,
    "F": world_f_survivorship,
    "G": world_g_multiple_testing,
}


def build_world(world_id: str, **kwargs: Any) -> SyntheticWorld:
    """Build a registered world with overridable parameters."""
    normalized = str(world_id).strip().upper()
    try:
        builder = WORLDS[normalized]
    except KeyError as exc:
        raise ResearchInputError(
            f"unknown world {world_id!r}; available: {sorted(WORLDS)}"
        ) from exc
    return builder(**kwargs)
