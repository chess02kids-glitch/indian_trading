"""Deterministic VectorBT-backed portfolio simulation with a safe pandas fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from math import isfinite, sqrt
from typing import Any

import numpy as np
import pandas as pd

from research.contracts import CostModel, ResearchInputError

from .metrics import PerformanceMetrics, compute_performance_metrics

try:  # VectorBT is an optional runtime backend for environments without JIT support.
    import vectorbt as _vectorbt
except Exception:  # pragma: no cover - depends on the deployment's numerical stack
    _vectorbt = None


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Backtest frequency, capital, cost, and volatility-targeting settings."""

    rebalance_frequency: str = "M"
    initial_cash: float = 1.0
    periods_per_year: int = 252
    cost_model: CostModel = CostModel()
    volatility_target: float | None = None
    volatility_lookback: int = 63
    max_leverage: float = 1.0
    use_vectorbt: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.rebalance_frequency, str)
            or not self.rebalance_frequency.strip()
        ):
            raise ResearchInputError("rebalance_frequency must be a non-empty string")
        frequency = (
            "ME"
            if self.rebalance_frequency.upper() == "M"
            else self.rebalance_frequency
        )
        try:
            pd.tseries.frequencies.to_offset(frequency)
        except ValueError as exc:
            raise ResearchInputError(
                "rebalance_frequency is not a valid pandas frequency"
            ) from exc
        if not isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ResearchInputError("initial_cash must be finite and positive")
        if self.periods_per_year < 1:
            raise ResearchInputError("periods_per_year must be positive")
        if self.volatility_target is not None and (
            not isfinite(self.volatility_target) or self.volatility_target <= 0
        ):
            raise ResearchInputError(
                "volatility_target must be finite and positive when supplied"
            )
        if self.volatility_lookback < 2:
            raise ResearchInputError("volatility_lookback must be at least two")
        if not isfinite(self.max_leverage) or self.max_leverage <= 0:
            raise ResearchInputError("max_leverage must be finite and positive")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """Return, allocation, trade, and metric outputs from one backtest."""

    strategy_name: str
    returns: pd.Series
    equity_curve: pd.Series
    weights: pd.DataFrame
    trades: pd.DataFrame
    metrics: PerformanceMetrics
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a compact machine-readable result summary."""
        return {
            "strategy": self.strategy_name,
            "metrics": self.metrics.to_dict(),
            "metadata": dict(self.metadata),
            "start": self.returns.index[0].isoformat(),
            "end": self.returns.index[-1].isoformat(),
        }


class VectorBTResearchEngine:
    """Run target-weight research simulations with VectorBT when available.

    Target weights are sampled at the configured rebalance dates and applied to
    the following period, preventing same-bar look-ahead. The deterministic
    pandas implementation is retained as a production fallback when VectorBT's
    numerical backend cannot import in a deployment.
    """

    def __init__(
        self,
        config: BacktestConfig | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.logger = logger or logging.getLogger(__name__)

    @property
    def vectorbt_available(self) -> bool:
        """Return whether VectorBT imported successfully in this environment."""
        return _vectorbt is not None

    def _market_cost_bps(self) -> float:
        """Market-dependent cost rate: spread+slippage when the cost model
        provides one (e.g. IndiaCostModel), else plain slippage_bps."""
        cost_model = self.config.cost_model
        market = getattr(cost_model, "market_cost_bps", None)
        if market is not None:
            return float(market)
        return float(cost_model.slippage_bps)

    @staticmethod
    def _validate_inputs(
        prices: pd.DataFrame, target_weights: pd.DataFrame
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        if not isinstance(prices, pd.DataFrame) or prices.empty:
            raise ResearchInputError("prices must be a non-empty DataFrame")
        if not isinstance(target_weights, pd.DataFrame) or target_weights.empty:
            raise ResearchInputError("target_weights must be a non-empty DataFrame")
        if not isinstance(prices.index, pd.DatetimeIndex) or not prices.index.is_unique:
            raise ResearchInputError("prices must use a unique DatetimeIndex")
        if not prices.index.is_monotonic_increasing:
            raise ResearchInputError("prices index must be sorted")
        if not prices.columns.is_unique:
            raise ResearchInputError("prices columns must be unique")
        if not prices.index.equals(target_weights.index) or not prices.columns.equals(
            target_weights.columns
        ):
            raise ResearchInputError("prices and target_weights must align exactly")
        numeric_prices = prices.apply(pd.to_numeric, errors="coerce")
        numeric_weights = target_weights.apply(pd.to_numeric, errors="coerce").fillna(
            0.0
        )
        if (
            numeric_prices.isna().any().any()
            or (numeric_prices <= 0).any().any()
            or not np.isfinite(numeric_prices.to_numpy()).all()
        ):
            raise ResearchInputError("prices must be finite and strictly positive")
        if (
            numeric_weights.isna().any().any()
            or not np.isfinite(numeric_weights.to_numpy()).all()
        ):
            raise ResearchInputError("target_weights must be finite numeric values")
        return numeric_prices.astype(float), numeric_weights.astype(float)

    @staticmethod
    def _rebalance_mask(index: pd.DatetimeIndex, frequency: str) -> pd.Series:
        periods = index.to_period(frequency)
        mask = pd.Series(~periods.duplicated(keep="last"), index=index)
        mask.iloc[0] = True
        return mask

    def _apply_volatility_target(
        self,
        targets: pd.DataFrame,
        asset_returns: pd.DataFrame,
        rebalance: pd.Series,
    ) -> pd.DataFrame:
        if self.config.volatility_target is None:
            return targets
        adjusted = targets.copy()
        for row_number, is_rebalance in enumerate(rebalance.to_numpy()):
            if not is_rebalance:
                adjusted.iloc[row_number] = adjusted.iloc[row_number - 1]
                continue
            current = adjusted.iloc[row_number].copy()
            history = asset_returns.iloc[
                max(0, row_number - self.config.volatility_lookback) : row_number
            ]
            portfolio_volatility = 0.0
            if len(history) >= 2 and float(current.abs().sum()) > 0:
                portfolio_returns = history @ current
                portfolio_volatility = float(
                    portfolio_returns.std(ddof=1) * sqrt(self.config.periods_per_year)
                )
            scale = (
                min(
                    self.config.max_leverage,
                    self.config.volatility_target / portfolio_volatility,
                )
                if portfolio_volatility > 0
                else 1.0
            )
            adjusted.iloc[row_number] = current * scale
        return adjusted

    def _prepare_targets(
        self,
        weights: pd.DataFrame,
        asset_returns: pd.DataFrame,
    ) -> tuple[pd.DataFrame, pd.Series]:
        rebalance = self._rebalance_mask(weights.index, self.config.rebalance_frequency)
        targets = weights.where(rebalance, pd.NA).ffill().fillna(0.0)
        targets = self._apply_volatility_target(targets, asset_returns, rebalance)
        return targets, rebalance

    def _simulate_pandas(
        self,
        prices: pd.DataFrame,
        targets: pd.DataFrame,
        rebalance: pd.Series,
    ) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
        asset_returns = prices.pct_change().fillna(0.0)
        previous_targets = targets.shift(1).fillna(0.0)
        effective_weights = previous_targets
        turnover = (targets - previous_targets).abs().sum(axis=1).where(rebalance, 0.0)
        transaction_cost = (
            turnover * self.config.cost_model.transaction_cost_bps / 10_000
        )
        slippage = turnover * self._market_cost_bps() / 10_000
        costs = transaction_cost + slippage
        returns = (effective_weights * asset_returns).sum(axis=1) - costs
        returns.name = "returns"
        equity = self.config.initial_cash * (1.0 + returns).cumprod()
        trades = pd.DataFrame(
            {
                "rebalance": rebalance.astype(bool),
                "turnover": turnover,
                "transaction_cost": transaction_cost,
                "slippage": slippage,
                "total_cost": costs,
            },
            index=prices.index,
        )
        return returns, equity, trades

    def _run_vectorbt(
        self,
        prices: pd.DataFrame,
        targets: pd.DataFrame,
    ) -> tuple[pd.Series, pd.Series] | None:
        if not self.config.use_vectorbt or _vectorbt is None:
            return None
        try:
            portfolio = _vectorbt.Portfolio.from_orders(
                close=prices,
                size=targets,
                size_type="targetpercent",
                direction="both",
                fees=self.config.cost_model.transaction_cost_bps / 10_000,
                slippage=self._market_cost_bps() / 10_000,
                init_cash=self.config.initial_cash,
                cash_sharing=True,
                group_by=True,
                freq=self.config.rebalance_frequency,
            )
            returns = portfolio.returns()
            equity = portfolio.value()
            if isinstance(returns, pd.DataFrame):
                returns = returns.sum(axis=1)
            if isinstance(equity, pd.DataFrame):
                equity = equity.sum(axis=1)
            return returns.astype(float), equity.astype(float)
        except Exception as exc:  # fallback is explicit in result metadata and logs
            self.logger.warning(
                "vectorbt_backend_failed_using_pandas",
                extra={"operation": "backtest", "error": str(exc)},
            )
            return None

    def run(
        self,
        prices: pd.DataFrame,
        target_weights: pd.DataFrame,
        strategy_name: str = "strategy",
        universe_history: list[Any] | None = None,
    ) -> BacktestResult:
        """Run a deterministic target-weight backtest with costs and turnover."""
        if universe_history is None:
            raise ResearchInputError(
                "universe_history is required. Backtests must explicitly provide "
                "historical index membership to prevent survivorship bias. Do not "
                "use today's universe for history."
            )
        prices, weights = self._validate_inputs(prices, target_weights)
        asset_returns = prices.pct_change().fillna(0.0)
        targets, rebalance = self._prepare_targets(weights, asset_returns)
        pandas_returns, pandas_equity, trades = self._simulate_pandas(
            prices, targets, rebalance
        )
        vectorbt_output = self._run_vectorbt(prices, targets)
        if vectorbt_output is None:
            returns, equity, backend = pandas_returns, pandas_equity, "pandas"
        else:
            returns, equity, backend = (
                vectorbt_output[0],
                vectorbt_output[1],
                "vectorbt",
            )
        trade_count = int((trades["turnover"] > 0).sum())
        total_cost = float(trades["total_cost"].sum())
        metrics = compute_performance_metrics(
            returns,
            turnover=trades["turnover"],
            periods_per_year=self.config.periods_per_year,
            initial_value=self.config.initial_cash,
            total_cost=total_cost,
            trade_count=trade_count,
        )
        metadata = {
            "backend": backend,
            "rebalance_frequency": self.config.rebalance_frequency,
            "initial_cash": self.config.initial_cash,
            "transaction_cost_bps": self.config.cost_model.transaction_cost_bps,
            "slippage_bps": self._market_cost_bps(),
            "total_cost": total_cost,
            "trade_count": trade_count,
            "volatility_target": self.config.volatility_target,
        }
        breakdown = getattr(self.config.cost_model, "to_dict", None)
        if callable(breakdown):
            try:
                metadata["cost_model"] = breakdown()
            except Exception:  # cost metadata is optional; never fail a run
                pass
        return BacktestResult(
            strategy_name=strategy_name,
            returns=returns,
            equity_curve=equity,
            weights=targets,
            trades=trades,
            metrics=metrics,
            metadata=metadata,
        )
