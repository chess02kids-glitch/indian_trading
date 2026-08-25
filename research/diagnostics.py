"""Deterministic factor diagnostics for research validation.

Generates the diagnostic evidence a research team needs before trusting a
factor in production:

* **factor decay** — information coefficient (IC) of each factor against
  forward returns at multiple horizons;
* **sector exposure** — portfolio weight by sector over time;
* **turnover attribution** — which names and directions drive turnover;
* **rank stability** — cross-sectional rank autocorrelation between
  rebalance dates;
* **volatility contribution** — average marginal contribution of each name
  to portfolio volatility;
* **factor contribution breakdown** — least-squares attribution of
  portfolio returns to a set of factor portfolios.

All functions are pure and deterministic: no random number generation, no
wall clock, no network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from backtest.engine import BacktestResult
from research.contracts import ResearchInputError

__all__ = [
    "FactorDiagnostics",
    "factor_contribution_breakdown",
    "factor_decay",
    "rank_stability",
    "sector_exposure",
    "turnover_attribution",
    "volatility_contribution",
]


def _validate_factor_panel(values: pd.DataFrame, returns: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(values, pd.DataFrame) or values.empty:
        raise ResearchInputError("factor values must be a non-empty DataFrame")
    if not values.index.equals(returns.index) or not values.columns.equals(
        returns.columns
    ):
        raise ResearchInputError("factor values must align with the return panel")
    return values


def _validated_returns(returns: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(returns, pd.DataFrame) or returns.empty:
        raise ResearchInputError("returns must be a non-empty DataFrame")
    numeric = returns.apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any():
        raise ResearchInputError("returns must not contain missing values")
    return numeric.astype(float)


def factor_decay(
    factors: Mapping[str, pd.DataFrame],
    returns: pd.DataFrame,
    *,
    horizons: Sequence[int] = (1, 5, 21, 63),
    min_observations: int = 30,
) -> dict[str, dict[str, float]]:
    """Average cross-sectional IC of each factor at multiple forward horizons.

    ``factors`` maps a factor name to its panel (dates x symbols). The IC at
    horizon ``h`` is the Spearman rank correlation, computed per date across
    the cross-section, between the factor value at ``t`` and the *forward*
    return ``P(t+h) / P(t) - 1``. A decaying profile with horizon is the
    classic factor-decay signature; a flat or negative profile signals that
    the factor's signal does not persist.
    """
    if not factors:
        raise ResearchInputError("at least one factor panel is required")
    if any(not isinstance(h, int) or h < 1 for h in horizons):
        raise ResearchInputError("horizons must be positive integers")
    if min_observations < 5:
        raise ResearchInputError("min_observations must be at least five")
    returns = _validated_returns(returns)
    forward_returns = {
        horizon: returns.shift(-horizon) / returns - 1.0 for horizon in horizons
    }
    output: dict[str, dict[str, float]] = {}
    for name, factor_values in factors.items():
        factor_values = _validate_factor_panel(factor_values, returns)
        profile: dict[str, float] = {}
        for horizon in horizons:
            forward = forward_returns[horizon]
            ic_values: list[float] = []
            for date_label in factor_values.index:
                factor_row = factor_values.loc[date_label]
                return_row = forward.loc[date_label]
                valid = factor_row.notna() & return_row.notna()
                if int(valid.sum()) < min_observations:
                    continue
                correlation = factor_row[valid].corr(
                    return_row[valid], method="spearman"
                )
                if pd.notna(correlation):
                    ic_values.append(float(correlation))
            profile[str(horizon)] = float(np.mean(ic_values)) if ic_values else 0.0
        output[str(name)] = profile
    return output


def rank_stability(
    factors: Mapping[str, pd.DataFrame],
    *,
    rebalance_dates: Sequence[pd.Timestamp] | None = None,
) -> dict[str, float]:
    """Mean Spearman rank autocorrelation between consecutive rebalances.

    ``factors`` maps a factor name to its panel (dates x symbols). For each
    factor, the cross-sectional Spearman rank correlation between the
    values at consecutive rebalance dates is averaged. The stability of
    cross-sectional ranks (rather than raw values) is what matters for
    ranking-based strategies: low rank stability means the factor is a
    noisy "which name won last week" signal.
    """
    if not factors:
        raise ResearchInputError("at least one factor panel is required")
    output: dict[str, float] = {}
    for name, factor_values in factors.items():
        if not isinstance(factor_values, pd.DataFrame) or factor_values.empty:
            raise ResearchInputError("factor values must be a non-empty DataFrame")
        numeric = factor_values.apply(pd.to_numeric, errors="coerce")
        if rebalance_dates is None:
            dates = list(numeric.index)
        else:
            dates = [pd.Timestamp(value) for value in rebalance_dates]
            missing = [date for date in dates if date not in numeric.index]
            if missing:
                raise ResearchInputError(
                    "rebalance dates missing from factor index: "
                    + ", ".join(str(value) for value in missing)
                )
        correlations: list[float] = []
        for previous, current in zip(dates, dates[1:], strict=False):
            first = numeric.loc[previous]
            second = numeric.loc[current]
            valid = first.notna() & second.notna()
            if int(valid.sum()) < 5:
                continue
            correlation = first[valid].corr(second[valid], method="spearman")
            if pd.notna(correlation):
                correlations.append(float(correlation))
        output[str(name)] = float(np.mean(correlations)) if correlations else 0.0
    return output


def sector_exposure(
    weights: pd.DataFrame,
    sector_map: Mapping[str, str],
) -> dict[str, dict[str, float]]:
    """Aggregate portfolio weight exposure by sector over time.

    ``sector_map`` maps symbols to sector names; weights are expected to be
    the backtest target weights (one row per date). Returns per-sector
    average, final, minimum, and maximum weight, plus the number of distinct
    holdings per sector.
    """
    if not isinstance(weights, pd.DataFrame) or weights.empty:
        raise ResearchInputError("weights must be a non-empty DataFrame")
    if not sector_map:
        raise ResearchInputError("sector_map must not be empty")
    missing = [symbol for symbol in weights.columns if symbol not in sector_map]
    if missing:
        raise ResearchInputError(
            "sector_map is missing symbols: " + ", ".join(sorted(missing))
        )
    numeric = weights.apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any():
        raise ResearchInputError("weights must not contain missing values")
    sectors = sorted({sector_map[column] for column in numeric.columns})
    output: dict[str, dict[str, float]] = {}
    for sector in sectors:
        members = [column for column in numeric.columns if sector_map[column] == sector]
        exposure = numeric[members].sum(axis=1)
        output[sector] = {
            "average_weight": float(exposure.mean()),
            "final_weight": float(exposure.iloc[-1]),
            "min_weight": float(exposure.min()),
            "max_weight": float(exposure.max()),
            "num_holdings": float(len(members)),
        }
    return output


def turnover_attribution(result: BacktestResult) -> dict[str, Any]:
    """Per-symbol and per-side attribution of total portfolio turnover.

    Buys are increases in target weight at a rebalance; sells are decreases.
    ``share`` is the contribution to total turnover; names are sorted
    descending by share so the report highlights the dominant drivers.
    """
    weights = result.weights
    if weights.empty:
        raise ResearchInputError("result.weights must not be empty")
    numeric = weights.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    rebalance = result.trades["rebalance"].astype(bool)
    changes = numeric.diff().fillna(0.0).where(rebalance, 0.0)
    total_turnover = float(abs(changes).sum().sum())
    if total_turnover <= 0:
        return {
            "total_turnover": 0.0,
            "by_symbol": {},
            "by_side": {"buys": 0.0, "sells": 0.0},
        }
    by_symbol: dict[str, dict[str, float]] = {}
    for column in numeric.columns:
        symbol_changes = changes[column]
        buys = float(symbol_changes[symbol_changes > 0].sum())
        sells = float(-symbol_changes[symbol_changes < 0].sum())
        by_symbol[str(column)] = {
            "turnover": buys + sells,
            "buy_turnover": buys,
            "sell_turnover": sells,
            "share": (buys + sells) / total_turnover,
            "rebalances": int((symbol_changes != 0).sum()),
        }
    ordered = {
        name: by_symbol[name]
        for name in sorted(by_symbol, key=lambda key: -by_symbol[key]["turnover"])
    }
    buys_side = float(sum(entry["buy_turnover"] for entry in by_symbol.values()))
    sells_side = float(sum(entry["sell_turnover"] for entry in by_symbol.values()))
    return {
        "total_turnover": total_turnover,
        "by_symbol": ordered,
        "by_side": {"buys": buys_side, "sells": sells_side},
    }


def volatility_contribution(
    weights: pd.DataFrame,
    asset_returns: pd.DataFrame,
    *,
    window: int = 63,
    periods_per_year: int = 252,
) -> dict[str, float]:
    """Average marginal contribution of each name to portfolio volatility.

    Uses the Euler decomposition of the quadratic form ``w' Σ w``: the
    fractional contribution of asset ``i`` to portfolio variance is
    ``w_i (Σ w)_i / (w' Σ w)``. Contributions therefore sum to one per
    date — the share of portfolio risk each name explains. Only trailing
    observations (``window`` rows strictly before the evaluation date) are
    used, so there is no look-ahead. The reported value is the time-average
    over all dates with sufficient history.
    """
    if window < 2:
        raise ResearchInputError("window must be at least two")
    if periods_per_year < 1:
        raise ResearchInputError("periods_per_year must be positive")
    if not isinstance(weights, pd.DataFrame) or weights.empty:
        raise ResearchInputError("weights must be a non-empty DataFrame")
    if not isinstance(asset_returns, pd.DataFrame) or asset_returns.empty:
        raise ResearchInputError("asset_returns must be a non-empty DataFrame")
    if not weights.index.equals(asset_returns.index) or not weights.columns.equals(
        asset_returns.columns
    ):
        raise ResearchInputError("weights and asset_returns must align exactly")
    numeric_weights = weights.apply(pd.to_numeric, errors="coerce").fillna(0.0)
    numeric_returns = asset_returns.apply(pd.to_numeric, errors="coerce")
    if numeric_returns.isna().any().any():
        raise ResearchInputError("asset_returns must not contain missing values")
    contributions: list[pd.Series] = []
    for position in range(window, len(numeric_returns)):
        history = numeric_returns.iloc[position - window : position]
        current = numeric_weights.iloc[position - 1]
        if float(np.abs(current.to_numpy(dtype=float)).sum()) <= 0:
            continue
        covariance = history.cov()
        vector = current.to_numpy(dtype=float)
        variance = float(vector @ covariance.to_numpy() @ vector)
        if variance <= 0 or not np.isfinite(variance):
            continue
        marginal = covariance.to_numpy() @ vector
        contribution = vector * marginal / variance
        if not np.isfinite(contribution).all():
            continue
        contributions.append(pd.Series(contribution, index=weights.columns))
    if not contributions:
        return {str(column): 0.0 for column in weights.columns}
    average = pd.concat(contributions, axis=1).mean(axis=1)
    return {str(column): float(value) for column, value in average.items()}


def factor_contribution_breakdown(
    returns: pd.Series,
    factor_returns: Mapping[str, pd.Series],
) -> dict[str, Any]:
    """Least-squares attribution of portfolio returns to factor portfolios.

    Each factor portfolio return series is built externally (e.g. top-quintile
    long-only or long-short spread). Portfolio returns are regressed on the
    factor portfolios plus an intercept; each factor's contribution is its
    beta times the average factor return, and the residual is the unexplained
    component. The regression is deterministic (no random seed).
    """
    if not isinstance(returns, pd.Series) or returns.empty:
        raise ResearchInputError("returns must be a non-empty Series")
    if len(factor_returns) < 1:
        raise ResearchInputError("at least one factor portfolio is required")
    aligned: pd.DataFrame = pd.DataFrame({"portfolio": pd.to_numeric(returns)})
    for name, series in factor_returns.items():
        if not isinstance(series, pd.Series):
            raise ResearchInputError("factor returns must be pandas Series")
        aligned[name] = pd.to_numeric(series)
    aligned = aligned.dropna()
    if len(aligned) < len(factor_returns) + 3:
        raise ResearchInputError(
            "not enough observations to estimate factor contributions"
        )
    independent = aligned.drop(columns="portfolio").to_numpy(dtype=float)
    dependent = aligned["portfolio"].to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(dependent)), independent])
    coefficients, _, _, _ = np.linalg.lstsq(design, dependent, rcond=None)
    fitted = design @ coefficients
    residual = dependent - fitted
    contributions: dict[str, float] = {}
    for position, name in enumerate(aligned.columns.drop("portfolio")):
        beta = float(coefficients[position + 1])
        contribution = beta * float(independent[:, position].mean())
        contributions[str(name)] = {"beta": beta, "contribution": contribution}
    total_variance = float(np.var(dependent, ddof=1))
    residual_variance = float(np.var(residual, ddof=1))
    return {
        "factors": contributions,
        "residual": float(residual.mean()),
        "r_squared": (
            float(1.0 - residual_variance / total_variance)
            if total_variance > 0
            else 0.0
        ),
        "observations": int(len(dependent)),
    }


@dataclass(frozen=True, slots=True)
class FactorDiagnostics:
    """Complete deterministic diagnostics bundle for one research run."""

    factor_decay: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    rank_stability: Mapping[str, float] = field(default_factory=dict)
    sector_exposure: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    turnover_attribution: Mapping[str, Any] = field(default_factory=dict)
    volatility_contribution: Mapping[str, float] = field(default_factory=dict)
    contribution_breakdown: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible diagnostics mapping."""
        return {
            "factor_decay": {
                name: dict(profile) for name, profile in self.factor_decay.items()
            },
            "rank_stability": dict(self.rank_stability),
            "sector_exposure": {
                name: dict(values) for name, values in self.sector_exposure.items()
            },
            "turnover_attribution": dict(self.turnover_attribution),
            "volatility_contribution": dict(self.volatility_contribution),
            "contribution_breakdown": dict(self.contribution_breakdown),
        }

    def to_json(self) -> str:
        """Serialize as deterministic JSON."""
        import json

        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), default=str
        )
