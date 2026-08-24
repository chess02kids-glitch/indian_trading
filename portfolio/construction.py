"""Research-only portfolio construction and allocation constraints."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.contracts import MarketData, ResearchInputError, Signal


class AllocationError(ResearchInputError):
    """Raised when requested target weights cannot satisfy constraints."""


@dataclass(frozen=True, slots=True)
class AllocationConstraints:
    """Long-only or bounded-weight constraints for research allocations."""

    min_weight: float = 0.0
    max_weight: float = 1.0
    max_gross_leverage: float = 1.0
    long_only: bool = True

    def __post_init__(self) -> None:
        if self.min_weight < 0:
            raise AllocationError("min_weight cannot be negative")
        if self.max_weight <= 0 or self.max_weight < self.min_weight:
            raise AllocationError("max_weight must be positive and at least min_weight")
        if self.max_gross_leverage <= 0:
            raise AllocationError("max_gross_leverage must be positive")
        if self.long_only and self.max_gross_leverage < 1:
            raise AllocationError(
                "long-only allocations require max_gross_leverage >= 1"
            )


def _validate_weight_panel(weights: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(weights, pd.DataFrame) or weights.empty:
        raise AllocationError("weights must be a non-empty DataFrame")
    if not isinstance(weights.index, pd.DatetimeIndex):
        raise AllocationError("weights must use a DatetimeIndex")
    if not weights.index.is_unique or not weights.columns.is_unique:
        raise AllocationError("weights index and columns must be unique")
    numeric = weights.apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy()).all():
        raise AllocationError("weights must contain finite numeric values")
    return numeric.astype(float)


def _project_long_only(
    values: np.ndarray, constraints: AllocationConstraints
) -> np.ndarray:
    active = values > 0
    count = int(active.sum())
    output = np.zeros_like(values, dtype=float)
    if count == 0:
        return output
    if count * constraints.min_weight > 1 + 1e-10:
        raise AllocationError("active assets cannot satisfy min_weight")
    if count * constraints.max_weight < 1 - 1e-10:
        raise AllocationError("active assets cannot satisfy max_weight")
    base = np.where(active, values, 0.0)
    lower = np.where(active, constraints.min_weight, 0.0)
    upper = np.where(active, constraints.max_weight, 0.0)
    low_scale = 0.0
    high_scale = max(1.0, 1.0 / float(base[active].min()))
    for _ in range(80):
        scale = (low_scale + high_scale) / 2
        candidate = np.clip(scale * base, lower, upper)
        if candidate.sum() < 1:
            low_scale = scale
        else:
            high_scale = scale
    output = np.clip(high_scale * base, lower, upper)
    residual = 1.0 - output.sum()
    if abs(residual) > 1e-8:
        free = active & (output < upper - 1e-9)
        if not free.any():
            raise AllocationError("unable to satisfy allocation bounds")
        output[free] += residual / float(free.sum())
    return output


def apply_constraints(
    weights: pd.DataFrame,
    constraints: AllocationConstraints | None = None,
) -> pd.DataFrame:
    """Project each target-weight row onto the configured allocation bounds."""
    constraints = constraints or AllocationConstraints()
    validated = _validate_weight_panel(weights)
    result = np.zeros_like(validated.to_numpy(), dtype=float)
    for row_number, row in enumerate(validated.to_numpy()):
        if constraints.long_only:
            result[row_number] = _project_long_only(np.maximum(row, 0.0), constraints)
            continue
        clipped = np.clip(row, -constraints.max_weight, constraints.max_weight)
        gross = float(np.abs(clipped).sum())
        if gross == 0:
            continue
        target_gross = min(gross, constraints.max_gross_leverage)
        scaled = clipped / gross * target_gross
        if np.abs(scaled).sum() > constraints.max_gross_leverage + 1e-8:
            raise AllocationError("gross leverage constraint violated")
        result[row_number] = scaled
    return pd.DataFrame(result, index=validated.index, columns=validated.columns)


def equal_weight(
    signals: Signal | pd.DataFrame,
    constraints: AllocationConstraints | None = None,
) -> pd.DataFrame:
    """Allocate equal long-only weight to every positive signal."""
    values = signals.values if isinstance(signals, Signal) else signals
    if not isinstance(values, pd.DataFrame):
        raise AllocationError("signals must be a DataFrame or Signal")
    validated = _validate_weight_panel(values.fillna(0.0))
    active = (validated > 0).astype(float)
    return apply_constraints(active, constraints)


def inverse_volatility(
    prices: pd.DataFrame,
    signals: Signal | pd.DataFrame | None = None,
    window: int = 20,
    constraints: AllocationConstraints | None = None,
) -> pd.DataFrame:
    """Allocate by inverse prior realized volatility among active signals."""
    if window < 2:
        raise AllocationError("window must be at least two")
    validated_prices = _validate_weight_panel(prices)
    if signals is not None:
        signal_values = signals.values if isinstance(signals, Signal) else signals
        if not isinstance(signal_values, pd.DataFrame):
            raise AllocationError("signals must be a DataFrame or Signal")
        active = _validate_weight_panel(signal_values.fillna(0.0)) > 0
        if not active.index.equals(validated_prices.index) or not active.columns.equals(
            validated_prices.columns
        ):
            raise AllocationError("signals and prices must align")
    else:
        active = pd.DataFrame(
            1.0,
            index=validated_prices.index,
            columns=validated_prices.columns,
        )
    volatility = (
        validated_prices.pct_change().shift(1).rolling(window, min_periods=window).std()
    )
    raw = active.astype(float) / volatility.replace(0, np.nan)
    raw = raw.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    fallback = active.astype(float)
    raw = raw.where(raw.sum(axis=1).gt(0), fallback)
    return apply_constraints(raw, constraints)


@dataclass(frozen=True, slots=True)
class EqualWeightConstructor:
    """Portfolio constructor implementing equal-weight allocation."""

    constraints: AllocationConstraints = AllocationConstraints()

    def construct(self, signals: Signal, data: MarketData) -> pd.DataFrame:
        """Construct equal weights aligned to the supplied market data."""
        if not signals.values.index.equals(data.close.index):
            raise AllocationError("signals and market data must share an index")
        return equal_weight(signals, self.constraints)


@dataclass(frozen=True, slots=True)
class InverseVolatilityConstructor:
    """Portfolio constructor using inverse prior realized volatility."""

    window: int = 20
    constraints: AllocationConstraints = AllocationConstraints()

    def construct(self, signals: Signal, data: MarketData) -> pd.DataFrame:
        """Construct inverse-volatility weights without look-ahead."""
        if not signals.values.index.equals(data.close.index):
            raise AllocationError("signals and market data must share an index")
        return inverse_volatility(data.close, signals, self.window, self.constraints)


def risk_contributions(
    weights: pd.Series,
    covariance: pd.DataFrame,
) -> pd.Series:
    """Return each asset's fractional contribution to portfolio volatility."""
    if not weights.index.equals(covariance.index) or not covariance.index.equals(
        covariance.columns
    ):
        raise AllocationError("weights and covariance labels must align")
    vector = weights.to_numpy(dtype=float)
    matrix = covariance.to_numpy(dtype=float)
    variance = float(vector @ matrix @ vector)
    if variance <= 0 or not np.isfinite(variance):
        raise AllocationError(
            "covariance must produce positive finite portfolio variance"
        )
    marginal = matrix @ vector
    contributions = vector * marginal / variance
    return pd.Series(contributions, index=weights.index, dtype=float)


def risk_parity_weights(
    covariance: pd.DataFrame,
    *,
    max_iterations: int = 100,
    tolerance: float = 1e-8,
    constraints: AllocationConstraints | None = None,
) -> pd.Series:
    """Approximate equal-risk-contribution weights for a covariance matrix."""
    if not isinstance(covariance, pd.DataFrame) or covariance.empty:
        raise AllocationError("covariance must be a non-empty DataFrame")
    if not covariance.index.equals(covariance.columns):
        raise AllocationError("covariance index and columns must match")
    if max_iterations < 1 or tolerance <= 0:
        raise AllocationError("max_iterations and tolerance must be positive")
    if not np.isfinite(covariance.to_numpy(dtype=float)).all():
        raise AllocationError("covariance must be finite")
    labels = covariance.index
    weights = pd.Series(1.0 / len(labels), index=labels, dtype=float)
    for _ in range(max_iterations):
        contributions = risk_contributions(weights, covariance)
        target = contributions.sum() / len(labels)
        updated = weights * target / contributions.replace(0, np.nan)
        updated = updated.replace([np.inf, -np.inf], np.nan).fillna(weights)
        updated /= updated.sum()
        if float(np.max(np.abs(updated.to_numpy() - weights.to_numpy()))) < tolerance:
            weights = updated
            break
        weights = updated
    constrained = apply_constraints(
        pd.DataFrame(
            [weights.to_numpy()],
            columns=labels,
            index=pd.DatetimeIndex([pd.Timestamp("1970-01-01")]),
        ),
        constraints,
    )
    return constrained.iloc[0].set_axis(labels)
