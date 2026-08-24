"""Core immutable contracts shared by research, portfolio, and backtest modules."""

from __future__ import annotations

import hashlib
import json
import math
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import pandas as pd


class ResearchInputError(ValueError):
    """Raised when research inputs are not aligned, finite, or reproducible."""


def _validate_panel(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ResearchInputError(f"{name} must be a non-empty pandas DataFrame")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ResearchInputError(f"{name} must use a DatetimeIndex")
    if not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ResearchInputError(f"{name} index must be sorted and unique")
    if not frame.columns.is_unique:
        raise ResearchInputError(f"{name} columns must be unique")
    columns = [str(column).strip().upper() for column in frame.columns]
    if any(not column for column in columns) or len(set(columns)) != len(columns):
        raise ResearchInputError(f"{name} columns must be non-empty and unique")
    try:
        numeric = frame.apply(pd.to_numeric, errors="raise")
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(f"{name} must contain numeric values") from exc
    numeric = numeric.astype(float)
    numeric.columns = columns
    return numeric


@dataclass(frozen=True, slots=True)
class MarketData:
    """Aligned OHLCV panels used by factors and strategies."""

    close: pd.DataFrame
    high: pd.DataFrame | None = None
    low: pd.DataFrame | None = None
    volume: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        close = _validate_panel(self.close, "close")
        object.__setattr__(self, "close", close)
        for name in ("high", "low", "volume"):
            panel = getattr(self, name)
            if panel is None:
                continue
            validated = _validate_panel(panel, name)
            if not validated.index.equals(close.index) or not validated.columns.equals(
                close.columns
            ):
                raise ResearchInputError(f"{name} must align exactly with close")
            object.__setattr__(self, name, validated)

    def select(self, symbols: Sequence[str]) -> MarketData:
        """Return a new aligned market-data object restricted to symbols."""
        if any(not isinstance(symbol, str) for symbol in symbols):
            raise ResearchInputError("selected symbols must be strings")
        requested = tuple(symbol.strip().upper() for symbol in symbols)
        if not requested or any(not symbol for symbol in requested):
            raise ResearchInputError("selected symbols must be non-empty")
        if len(set(requested)) != len(requested):
            raise ResearchInputError("selected symbols must be unique")
        missing = set(requested) - set(self.close.columns)
        if missing:
            raise ResearchInputError(
                "symbols are missing from market data: " + ", ".join(sorted(missing))
            )
        columns = list(requested)
        return MarketData(
            close=self.close.loc[:, columns],
            high=self.high.loc[:, columns] if self.high is not None else None,
            low=self.low.loc[:, columns] if self.low is not None else None,
            volume=self.volume.loc[:, columns] if self.volume is not None else None,
        )

    @classmethod
    def from_long_frame(
        cls,
        frame: pd.DataFrame,
        *,
        date_column: str = "date",
        symbol_column: str = "symbol",
        close_column: str = "close",
        high_column: str = "high",
        low_column: str = "low",
        volume_column: str = "volume",
    ) -> MarketData:
        """Build aligned panels from a canonical long-form market-data frame."""
        required = {date_column, symbol_column, close_column}
        missing = required - set(frame.columns)
        if missing:
            raise ResearchInputError(
                "long frame is missing columns: " + ", ".join(sorted(missing))
            )
        working = frame.copy()
        working[date_column] = pd.to_datetime(working[date_column], errors="raise")
        if working.duplicated([date_column, symbol_column]).any():
            raise ResearchInputError("long frame contains duplicate date/symbol rows")

        def pivot(column: str) -> pd.DataFrame | None:
            return (
                working.pivot(index=date_column, columns=symbol_column, values=column)
                if column in working.columns
                else None
            )

        close = pivot(close_column)
        high = pivot(high_column)
        low = pivot(low_column)
        volume = pivot(volume_column)
        return cls(close=close, high=high, low=low, volume=volume)


@dataclass(frozen=True, slots=True)
class FactorMetadata:
    """Stable metadata describing one reproducible factor calculation."""

    name: str
    family: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    version: str = "1.0"

    def __post_init__(self) -> None:
        if (
            not self.name.strip()
            or not self.family.strip()
            or not self.description.strip()
        ):
            raise ResearchInputError(
                "factor metadata name, family, and description are required"
            )
        object.__setattr__(self, "parameters", dict(self.parameters))

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible factor metadata."""
        return {
            "name": self.name,
            "family": self.family,
            "description": self.description,
            "parameters": dict(self.parameters),
            "version": self.version,
        }


class Factor(ABC):
    """Abstract reproducible transformation from market data to a factor panel."""

    @property
    @abstractmethod
    def metadata(self) -> FactorMetadata:
        """Return the factor's name, family, version, and parameters."""

    @abstractmethod
    def compute(self, data: MarketData) -> pd.DataFrame:
        """Compute an index-aligned factor panel without mutating ``data``."""


class Strategy(ABC):
    """Common interface for strategy signal generation."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return a stable strategy name."""

    @property
    def parameters(self) -> Mapping[str, Any]:
        """Return serializable strategy parameters."""
        return {}

    @abstractmethod
    def generate_signals(self, data: MarketData) -> Signal:
        """Generate deterministic signal values aligned with market data."""


@dataclass(frozen=True, slots=True)
class Signal:
    """A named strategy signal panel with reproducibility metadata."""

    values: pd.DataFrame
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", _validate_panel(self.values, "signal"))
        object.__setattr__(self, "metadata", dict(self.metadata))


class PortfolioConstructor(Protocol):
    """Protocol for research-only signal-to-weight portfolio constructors."""

    def construct(self, signals: Signal, data: MarketData) -> pd.DataFrame:
        """Return target weights aligned with ``signals`` and ``data``."""


@dataclass(frozen=True, slots=True)
class CostModel:
    """Proportional transaction-cost and slippage assumptions."""

    transaction_cost_bps: float = 5.0
    slippage_bps: float = 2.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.transaction_cost_bps)
            or not math.isfinite(self.slippage_bps)
            or self.transaction_cost_bps < 0
            or self.slippage_bps < 0
        ):
            raise ResearchInputError(
                "cost and slippage must be finite and non-negative"
            )

    @property
    def proportional_rate(self) -> float:
        """Return combined cost and slippage as a decimal rate."""
        return (self.transaction_cost_bps + self.slippage_bps) / 10_000

    def cost(self, turnover: float) -> float:
        """Calculate cost for non-negative one-way turnover."""
        if turnover < 0:
            raise ResearchInputError("turnover must be non-negative")
        return float(turnover) * self.proportional_rate


@dataclass(frozen=True, slots=True)
class Experiment:
    """Experiment specification used by local reports and MLflow tracking."""

    hypothesis_id: str
    strategy: str
    parameters: Mapping[str, Any]
    factor_set: Sequence[str]
    universe: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    dataset_version: str | None = None
    cost_model: str | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.hypothesis_id, "hypothesis_id"),
            (self.strategy, "strategy"),
            (self.universe, "universe"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ResearchInputError(f"{field_name} must be non-empty")
        for value, field_name in (
            (self.dataset_version, "dataset_version"),
            (self.cost_model, "cost_model"),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ResearchInputError(f"{field_name} must be a non-empty string")
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "factor_set", tuple(self.factor_set))

    @property
    def experiment_id(self) -> str:
        """Return a stable hash for hypothesis, strategy, parameters, and universe."""
        payload = {
            "hypothesis_id": self.hypothesis_id,
            "strategy": self.strategy,
            "parameters": self.parameters,
            "factor_set": self.factor_set,
            "universe": self.universe,
        }
        # Optional provenance fields participate in the identity only when set,
        # keeping pre-existing experiment ids stable.
        if self.dataset_version is not None:
            payload["dataset_version"] = self.dataset_version
        if self.cost_model is not None:
            payload["cost_model"] = self.cost_model
        encoded = json.dumps(
            payload, sort_keys=True, default=str, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable experiment specification."""
        return {
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "strategy": self.strategy,
            "parameters": dict(self.parameters),
            "factor_set": list(self.factor_set),
            "universe": self.universe,
            "created_at": self.created_at.isoformat(),
            "dataset_version": self.dataset_version,
            "cost_model": self.cost_model,
        }
