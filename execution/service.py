"""Deterministic execution service.

Pipeline (strategy code never appears here):

    PortfolioTarget
        -> delta computation vs current positions
        -> OrderIntent construction (deterministic ids + idempotency keys)
        -> validate_order_intent (LIMIT-only choke point)
        -> risk guard (global state + per-order duplicate check)
        -> idempotency registry (duplicate rejection)
        -> execution adapter (paper broker)
        -> persisted intents/results

If the risk guard returns any protective state, no order is submitted
(fail closed).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Mapping

from models.domain import (
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioTarget,
)
from risk_kill import RiskDecision, RiskGuard, RiskState
from store.protocols import OrderRepository, PositionRepository

from .idempotency import IdempotencyRegistry, compute_idempotency_key
from .validation import validate_order_intent

__all__ = ["ExecutionService", "ExecutionSummary"]


def _order_id_for(target: PortfolioTarget, symbol: str, side: str) -> str:
    """Deterministic internal order id for one target/symbol/side."""
    basis = (
        f"{target.strategy_id}|{target.hypothesis_id}|"
        f"{target.as_of.isoformat()}|{symbol}|{side}"
    )
    return "ord-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


@dataclass
class ExecutionSummary:
    """Outcome of one execution pass."""

    run_id: str
    risk_state: str
    submitted: list[OrderResult] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    halted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "risk_state": self.risk_state,
            "halted": self.halted,
            "submitted": [
                {
                    "internal_order_id": r.internal_order_id,
                    "symbol": r.symbol,
                    "status": r.status.value,
                    "filled_quantity": r.filled_quantity,
                    "average_fill_price": r.average_fill_price,
                    "reason": r.reason,
                }
                for r in self.submitted
            ],
            "skipped": list(self.skipped),
        }


class ExecutionService:
    """Builds and submits orders for a portfolio target, guarded by risk."""

    def __init__(
        self,
        *,
        broker: Any,
        order_repository: OrderRepository,
        position_repository: PositionRepository,
        risk_guard: RiskGuard,
        idempotency_registry: IdempotencyRegistry | None = None,
        rebalance_date: date | None = None,
        health_service: Any = None,
        alert_service: Any = None,
    ) -> None:
        self.broker = broker
        self.order_repository = order_repository
        self.position_repository = position_repository
        self.risk_guard = risk_guard
        self.registry = idempotency_registry or IdempotencyRegistry()
        self._rebalance_date = rebalance_date
        self.health_service = health_service
        self.alert_service = alert_service
        self._lock = threading.Lock()

    def _current_position(self, symbol: str) -> int:
        position = self.position_repository.get_position(symbol)
        return position.quantity if position is not None else 0

    def build_intents(
        self,
        target: PortfolioTarget,
        current_positions: Mapping[str, int] | None,
        rebalance_date: date | None,
        *,
        now: datetime | None = None,
    ) -> list[OrderIntent]:
        """Compute the order intents that achieve the target state."""
        moment = now or datetime.now(UTC)
        positions = dict(current_positions or {})
        intents: list[OrderIntent] = []
        symbols = sorted(set(target.limits) | (set(target.target_quantities or {})))
        for symbol in symbols:
            if symbol not in target.limits:
                continue
            desired = (target.target_quantities or {}).get(symbol, 0)
            current = positions.get(symbol, 0)
            delta = desired - current
            if delta == 0:
                continue
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            quantity = abs(delta)
            order_id = _order_id_for(target, symbol, side.value)
            key = compute_idempotency_key(
                {
                    "strategy_id": target.strategy_id,
                    "hypothesis_id": target.hypothesis_id,
                    "symbol": symbol,
                    "side": side.value,
                    "quantity": quantity,
                    "limit_price": target.limits[symbol],
                    "order_type": OrderType.LIMIT.value,
                    "rebalance_date": (
                        self._rebalance_date or rebalance_date
                    ).isoformat(),
                }
            )
            intents.append(
                OrderIntent.model_validate(
                    {
                        "internal_order_id": order_id,
                        "idempotency_key": key,
                        "strategy_id": target.strategy_id,
                        "hypothesis_id": target.hypothesis_id,
                        "symbol": symbol,
                        "exchange": "NSE",
                        "side": side,
                        "quantity": quantity,
                        "limit_price": target.limits[symbol],
                        "order_type": OrderType.LIMIT,
                        "timestamp": moment,
                        "target_position": desired,
                    }
                )
            )
        return intents

    def execute_targets(
        self,
        target: PortfolioTarget,
        *,
        run_id: str,
        reference_prices: Mapping[str, float],
        risk_context: Any,
        now: datetime | None = None,
    ) -> ExecutionSummary:
        """Run the full guarded execution pass for one portfolio target."""
        moment = now or datetime.now(UTC)
        with self._lock:
            decision: RiskDecision = self.risk_guard.evaluate(risk_context)
            if decision.state is not RiskState.NOMINAL:
                if self.health_service:
                    from observability.health import SystemHealth

                    if decision.state == RiskState.HALTED:
                        self.health_service.set_state(
                            SystemHealth.HALTED,
                            reason=f"Risk state {decision.state.value}",
                        )
                    elif decision.state == RiskState.LOCKED:
                        self.health_service.set_state(
                            SystemHealth.LOCKED,
                            reason=f"Risk state {decision.state.value}",
                        )
                    elif decision.state == RiskState.WARNING:
                        self.health_service.set_state(
                            SystemHealth.WARNING,
                            reason=f"Risk state {decision.state.value}",
                        )

                if self.alert_service:
                    if decision.state in (RiskState.HALTED, RiskState.LOCKED):
                        self.alert_service.critical(
                            f"risk_{decision.state.value.lower()}",
                            message=f"Risk guard triggered: {decision.state.value}",
                            run_id=run_id,
                        )
                    else:
                        self.alert_service.warning(
                            f"risk_{decision.state.value.lower()}",
                            message=f"Risk guard warned: {decision.state.value}",
                            run_id=run_id,
                        )
                return ExecutionSummary(
                    run_id=run_id,
                    risk_state=decision.state.value,
                    halted=True,
                    skipped=[
                        {
                            "symbol": symbol,
                            "reason": f"risk state {decision.state.value}",
                        }
                        for symbol in sorted(target.limits)
                    ],
                )

            positions = {
                p.symbol: p.quantity for p in self.position_repository.list_positions()
            }
            intents = self.build_intents(target, positions, target.as_of, now=moment)
            summary = ExecutionSummary(run_id=run_id, risk_state=decision.state.value)
            for intent in intents:
                validated = validate_order_intent(intent)
                duplicate = self.risk_guard.check_duplicate_order(
                    validated.idempotency_key, self.registry.accepted_keys()
                )
                if duplicate is not None:
                    summary.skipped.append(
                        {
                            "symbol": intent.symbol,
                            "reason": "duplicate idempotency key",
                        }
                    )
                    continue
                claim = self.registry.claim(validated.idempotency_key)
                if not claim.accepted:
                    summary.skipped.append(
                        {
                            "symbol": intent.symbol,
                            "reason": f"already handled ({claim.reason})",
                        }
                    )
                    continue
                self.order_repository.save_intent(validated)
                reference = reference_prices.get(intent.symbol)
                if reference is None:
                    self.registry.mark_completed(validated.idempotency_key)
                    summary.skipped.append(
                        {
                            "symbol": intent.symbol,
                            "reason": "no reference price; not executed",
                        }
                    )
                    continue
                result = self.broker.submit_order(validated, float(reference))
                self.order_repository.save_result(result)
                if result.status in (
                    OrderStatus.FILLED,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.REJECTED,
                    OrderStatus.CANCELLED,
                    OrderStatus.EXPIRED,
                ):
                    self.registry.mark_completed(validated.idempotency_key)
                summary.submitted.append(result)
            self._sync_positions(target)

            if self.health_service:
                self.health_service.write_extended_status(
                    {
                        "latest_run": run_id,
                        "risk_state": decision.state.value,
                        "open_orders": [
                            {
                                "internal_order_id": r.internal_order_id,
                                "symbol": r.symbol,
                                "status": r.status.value,
                                "filled_quantity": r.filled_quantity,
                            }
                            for r in summary.submitted
                            if r.status
                            in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
                        ],
                        "paper_positions": [
                            {"symbol": p.symbol, "quantity": p.quantity}
                            for p in self.position_repository.list_positions()
                        ],
                    }
                )

            return summary

    def _sync_positions(self, target: PortfolioTarget) -> None:
        """Persist broker-side positions into the position repository."""
        for position in self.broker.get_positions():
            self.position_repository.upsert_position(position)
