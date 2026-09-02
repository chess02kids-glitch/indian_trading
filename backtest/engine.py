"""Deterministic VectorBT-backed portfolio simulation with a safe pandas fallback."""

from __future__ import annotations

import logging
from collections.abc import Iterable as IterableABC
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from datetime import date, datetime
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

__all__ = [
    "MEMBERSHIP_FROM_PRICES",
    "BacktestConfig",
    "BacktestResult",
    "VectorBTResearchEngine",
]


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
    #: Which simulation produces the numbers that are *reported* and used by
    #: the research gate. AUDIT-008: VectorBT and the pandas implementation
    #: disagree by up to 17% of total return on the same inputs, so a gate
    #: decision could depend on whether an optional dependency happened to
    #: import. ``pandas`` is deterministic and always available.
    report_backend: str = "pandas"
    #: AUDIT-009: never invent a price. When False (the default) a panel with
    #: gaps raises instead of forward-filling through them.
    allow_price_fill: bool = False

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
        if self.report_backend not in ("pandas", "vectorbt"):
            raise ResearchInputError(
                "report_backend must be 'pandas' or 'vectorbt', got "
                f"{self.report_backend!r}"
            )


#: Sentinel for :meth:`VectorBTResearchEngine.run`'s ``universe_history``.
#:
#: "A symbol is a member on date *t* if the panel has a price for it on *t*."
#: This is **weaker** than a real point-in-time membership history — a symbol
#: that was delisted before the panel starts cannot be represented at all —
#: but it is honest about what it is, and it is far better than the empty
#: list, which silently means "no protection at all". Use it only for panels
#: where no membership history exists (placebos, benchmarks computed on the
#: same fixed panel as the candidate).
MEMBERSHIP_FROM_PRICES = "from_prices"


def _coerce_membership(
    universe_history: Any,
    *,
    index: pd.DatetimeIndex,
    columns: pd.Index,
    prices: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the boolean (date x symbol) membership mask for a backtest.

    AUDIT-007. ``VectorBTResearchEngine.run`` *required* ``universe_history``
    and then never used it: a backtest run on today's index constituents over
    ten years of history was scored exactly as if those constituents had been
    known in advance. This converts the argument into the mask and the engine
    applies it.

    Accepted forms
    --------------
    * a boolean (or 0/1) :class:`~pandas.DataFrame` aligned to the panel;
    * a sequence of ``(date, members)`` pairs — membership is stepped forward
      from each date until the next one;
    * a sequence of records with ``symbol`` / ``valid_from`` / ``valid_to``
      keys (``UniverseDataset.to_frame().to_dict("records")``);
    * a single frozen snapshot: a sequence of symbols, or a one-element
      sequence containing one (``research.universe.Universe.history``);
    * the string :data:`MEMBERSHIP_FROM_PRICES`.

    ``None`` and an empty sequence are **rejected**: both used to mean "no
    protection", and that is the failure mode this guard exists to prevent.
    Dates or symbols the mask does not mention are treated as *not members*
    — fail closed.
    """
    if universe_history is None:
        raise ResearchInputError(
            "universe_history is required. Backtests must explicitly provide "
            "historical index membership to prevent survivorship bias. Do not "
            "use today's universe for history."
        )
    if isinstance(universe_history, str):
        if universe_history != MEMBERSHIP_FROM_PRICES:
            raise ResearchInputError(
                "universe_history must be a membership history, a boolean "
                f"DataFrame, or {MEMBERSHIP_FROM_PRICES!r}; got "
                f"{universe_history!r}"
            )
        if prices is None:
            raise ResearchInputError(
                f"{MEMBERSHIP_FROM_PRICES!r} requires the price panel"
            )
        return prices.notna()
    if isinstance(universe_history, pd.DataFrame):
        frame = universe_history
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ResearchInputError("membership DataFrame must use a DatetimeIndex")
        aligned = frame.reindex(index=index, columns=columns)
        return aligned.fillna(False).astype(bool)

    records = list(universe_history)
    if not records:
        raise ResearchInputError(
            "universe_history is empty. An empty history means 'no protection' "
            "and is exactly the survivorship bias this argument exists to "
            "prevent; pass a real membership history, a boolean DataFrame, or "
            f"{MEMBERSHIP_FROM_PRICES!r}."
        )
    upper = {str(column).upper(): column for column in columns}
    if all(isinstance(record, MappingABC) for record in records):
        return _mask_from_validity_records(records, index=index, upper=upper)
    if all(_looks_like_membership_pair(record) for record in records):
        return _mask_from_dated_pairs(records, index=index, upper=upper)
    # A frozen snapshot: either the symbols themselves, or one sequence of
    # them (research.universe.Universe.history returns `[self.symbols]`).
    snapshot = records[0] if len(records) == 1 and not isinstance(records[0], str) else records
    if isinstance(snapshot, str) or not isinstance(snapshot, IterableABC):
        raise ResearchInputError(
            "universe_history must be a membership history, a boolean "
            f"DataFrame, or {MEMBERSHIP_FROM_PRICES!r}"
        )
    members = {str(symbol).strip().upper() for symbol in snapshot}
    mask = pd.DataFrame(False, index=index, columns=columns)
    for name, column in upper.items():
        if name in members:
            mask[column] = True
    return mask


def _looks_like_membership_pair(record: Any) -> bool:
    """True for a ``(date, members)`` pair.

    A frozen snapshot is often written as ``[("A", "B", "C")]``, which is also
    a two-or-more element tuple, so the discriminator is whether the first
    element parses as a timestamp and the second is a collection of symbols.
    """
    if not isinstance(record, (tuple, list)) or len(record) != 2:
        return False
    date_value, members = record
    if isinstance(members, str) or not isinstance(members, IterableABC):
        return False
    if isinstance(date_value, (pd.Timestamp, datetime)):
        return True
    if not isinstance(date_value, (str, date)):
        return False
    try:
        pd.Timestamp(date_value)
    except (TypeError, ValueError):
        return False
    return True


def _mask_from_validity_records(
    records: list[Any],
    *,
    index: pd.DatetimeIndex,
    upper: MappingABC[str, Any],
) -> pd.DataFrame:
    mask = pd.DataFrame(False, index=index, columns=list(upper.values()))
    for record in records:
        symbol = str(record.get("symbol", "")).strip().upper()
        column = upper.get(symbol)
        if column is None:
            continue
        start = record.get("valid_from") or record.get("date") or record.get("as_of")
        end = record.get("valid_to")
        try:
            start_ts = pd.Timestamp(start) if start is not None else None
            end_ts = pd.Timestamp(end) if end is not None else None
        except (TypeError, ValueError):
            continue
        if start_ts is None:
            continue
        if end_ts is not None and end_ts < index[0]:
            continue
        slice_ = index.slice_indexer(
            max(start_ts, index[0]), min(end_ts, index[-1]) if end_ts is not None else index[-1]
        )
        mask.iloc[slice_, mask.columns.get_loc(column)] = True
    return mask


def _mask_from_dated_pairs(
    records: list[Any],
    *,
    index: pd.DatetimeIndex,
    upper: MappingABC[str, Any],
) -> pd.DataFrame:
    pairs = []
    for date_value, members in records:
        if isinstance(members, str) or not isinstance(members, IterableABC):
            raise ResearchInputError("universe_history pairs must be (date, members)")
        pairs.append((pd.Timestamp(date_value), {str(s).strip().upper() for s in members}))
    pairs.sort(key=lambda pair: pair[0])
    frame = pd.DataFrame(
        {
            column: pd.Series(
                [name in members for _, members in pairs],
                index=pd.DatetimeIndex([day for day, _ in pairs]),
            )
            for name, column in upper.items()
        }
    )
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    aligned = frame.astype("boolean").reindex(index).ffill().fillna(False).astype(bool)
    return aligned[list(upper.values())]


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
        prices: pd.DataFrame,
        target_weights: pd.DataFrame,
        *,
        allow_price_fill: bool = False,
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
        # AUDIT-009: this used to be ``.ffill().bfill()``, which invented a
        # price for every gap — including every date before a symbol listed
        # (research.realdata does exactly that one level up, AUDIT-014). A
        # backtest must never silently trade a price that did not exist.
        if numeric_prices.isna().any().any():
            if not allow_price_fill:
                gap_count = int(numeric_prices.isna().to_numpy().sum())
                raise ResearchInputError(
                    f"prices contain {gap_count} gaps; the system does not "
                    "impute prices (see data.quality) — mask or exclude the "
                    "affected symbols first, or set "
                    "BacktestConfig(allow_price_fill=True) to accept the "
                    "historical behaviour explicitly"
                )
            numeric_prices = numeric_prices.ffill().bfill()
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
        universe_history: Any = None,
    ) -> BacktestResult:
        """Run a deterministic target-weight backtest with costs and turnover.

        ``universe_history`` is mandatory and is **applied**: a symbol that was
        not a member of the universe on date *t* is given weight zero at *t*
        (see :func:`_coerce_membership`, AUDIT-007).
        """
        prices, weights = self._validate_inputs(
            prices, target_weights, allow_price_fill=self.config.allow_price_fill
        )
        # AUDIT-007: the membership mask is applied to the *weights*, which is
        # what survivorship bias actually distorts — a symbol must not be held
        # on a date when it was not in the universe. Prices are left intact
        # (a zero weight already removes their contribution, and masking the
        # price panel instead would poison the volatility-target estimate with
        # NaNs).
        membership = _coerce_membership(
            universe_history, index=prices.index, columns=prices.columns, prices=prices
        )
        weights = weights.where(membership, 0.0)
        asset_returns = prices.pct_change().fillna(0.0)
        targets, rebalance = self._prepare_targets(weights, asset_returns)
        pandas_returns, pandas_equity, trades = self._simulate_pandas(
            prices, targets, rebalance
        )
        vectorbt_output = self._run_vectorbt(prices, targets)
        divergence: float | None = None
        if vectorbt_output is None:
            returns, equity, backend = pandas_returns, pandas_equity, "pandas"
        else:
            # AUDIT-008: both backends are computed, but only one is allowed to
            # produce a reported number. Otherwise the same inputs give a
            # different Sharpe depending on whether an optional dependency
            # imported, and the research gate can approve a strategy on one
            # machine and reject it on another.
            vectorbt_returns, vectorbt_equity = vectorbt_output
            divergence = float(
                abs(
                    float(vectorbt_equity.iloc[-1] if len(vectorbt_equity) else 0.0)
                    - float(pandas_equity.iloc[-1] if len(pandas_equity) else 0.0)
                )
            )
            if self.config.report_backend == "vectorbt":
                returns, equity, backend = (
                    vectorbt_returns,
                    vectorbt_equity,
                    "vectorbt",
                )
            else:
                returns, equity, backend = pandas_returns, pandas_equity, "pandas"
                if divergence > 1e-6:
                    self.logger.warning(
                        "backtest_backend_divergence",
                        extra={
                            "operation": "backtest",
                            "strategy": strategy_name,
                            "pandas_final_equity": float(pandas_equity.iloc[-1]),
                            "vectorbt_final_equity": float(vectorbt_equity.iloc[-1]),
                            "absolute_difference": divergence,
                        },
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
        coverage = float(membership.to_numpy().mean()) if membership.size else 0.0
        metadata = {
            "backend": backend,
            "report_backend": self.config.report_backend,
            "cross_checked": vectorbt_output is not None,
            "backend_divergence_final_equity": (
                None if divergence is None else round(divergence, 10)
            ),
            "membership_coverage": round(coverage, 6),
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
