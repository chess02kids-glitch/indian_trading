"""Daily orchestration pipeline: the safe end-to-end flow.

    data validation
        -> research (signals)
        -> portfolio allocation (targets)
        -> risk checks (risk_kill)
        -> HUMAN APPROVAL (fail-closed gate)
        -> paper execution (LIMIT-only, idempotent)
        -> fill reconciliation
        -> EOD reconciliation
        -> health status

The strategy is an input to this pipeline, never a caller of the broker.
Every fail condition stops the day's trading (fail closed) and is recorded
in the run repository, the health document, and the alert log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Mapping, Protocol, runtime_checkable

import pandas as pd

from data.quality import detect_data_staleness, validate_market_bars
from execution.paper import PaperBroker
from execution.service import ExecutionService, ExecutionSummary
from models.domain import (
    PortfolioTarget,
    ReconciliationResult,
    ResearchResult,
)
from observability.alerts import AlertService
from observability.health import HealthService, SystemHealth
from reconciliation.engine import ReconciliationEngine, ReconciliationInput
from research.contracts import MarketData, Signal, Strategy
from research.ledger import HypothesisLedger
from risk_kill import RiskContext, RiskGuard, RiskState
from store.protocols import (
    OrderRepository,
    PositionRepository,
    ReconciliationRepository,
    ResearchRepository,
    RunRepository,
)

__all__ = [
    "ApprovalGate",
    "DailyPipeline",
    "DailyRunResult",
    "ExecutionPlan",
    "ManualApprovalGate",
    "RecordingApprovalGate",
]


@runtime_checkable
class ApprovalGate(Protocol):
    """Human approval boundary. Execution happens only when approve() is True."""

    def approve(self, run_id: str, plan: "ExecutionPlan") -> bool: ...


@dataclass(frozen=True)
class ExecutionPlan:
    """What the human is being asked to approve before orders are sent."""

    run_id: str
    strategy_id: str
    hypothesis_id: str
    risk_state: str
    orders: tuple[Mapping[str, Any], ...] = ()
    data_quality: Mapping[str, Any] | None = None


class ManualApprovalGate:
    """Fail-closed gate: nothing is approved until a human explicitly grants
    approval for a specific run id."""

    def __init__(self) -> None:
        self._granted: set[str] = set()
        self._requests: list[ExecutionPlan] = []

    def grant_approval(self, run_id: str) -> None:
        """The explicit human action (operator UI, chat command, ...)."""
        self._granted.add(run_id)

    def approve(self, run_id: str, plan: ExecutionPlan) -> bool:
        self._requests.append(plan)
        return run_id in self._granted

    @property
    def pending(self) -> list[ExecutionPlan]:
        return list(self._requests)


class RecordingApprovalGate(ManualApprovalGate):
    """Gate that auto-approves for supervised paper runs and tests.

    It records every plan it approves, so the approval audit trail is still
    complete. Never use it as the only control for live trading.
    """

    def __init__(self) -> None:
        super().__init__()
        self.auto_approve = True

    def approve(self, run_id: str, plan: ExecutionPlan) -> bool:
        approved = self.auto_approve or super().approve(run_id, plan)
        if approved:
            self._granted.add(run_id)
        return approved


@dataclass
class DailyRunResult:
    """Outcome of one daily pipeline run."""

    run_id: str
    status: str
    health: str
    risk_state: str
    approved: bool
    execution: ExecutionSummary | None = None
    reconciliation: ReconciliationResult | None = None
    data_quality: Mapping[str, Any] | None = None
    signals_generated: bool = False
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "health": self.health,
            "risk_state": self.risk_state,
            "approved": self.approved,
            "signals_generated": self.signals_generated,
            "execution": self.execution.to_dict() if self.execution else None,
            "reconciliation": (
                {
                    "matched": self.reconciliation.matched,
                    "locked": self.reconciliation.locked,
                    "mismatches": [
                        {
                            "kind": m.kind,
                            "symbol": m.symbol,
                            "expected": m.expected,
                            "actual": m.actual,
                        }
                        for m in self.reconciliation.mismatches
                    ],
                }
                if self.reconciliation
                else None
            ),
            "data_quality": dict(self.data_quality or {}),
            "metrics": dict(self.metrics),
        }


class DailyPipeline:
    """Wires the deterministic components into the daily safe flow."""

    def __init__(
        self,
        *,
        strategy: Strategy,
        constructor: Any,
        broker: PaperBroker,
        execution_service: ExecutionService,
        risk_guard: RiskGuard | None = None,
        run_repository: RunRepository,
        position_repository: PositionRepository,
        order_repository: OrderRepository,
        research_repository: ResearchRepository,
        reconciliation_repository: ReconciliationRepository,
        reconciliation_engine: ReconciliationEngine | None = None,
        health_service: HealthService,
        alert_service: AlertService,
        approval_gate: ApprovalGate,
        ledger: HypothesisLedger | None = None,
        dataset_version: str = "unknown",
        max_staleness_days: float = 6.0,
        cash_for_allocation: float = 1_000_000.0,
    ) -> None:
        self.strategy = strategy
        self.constructor = constructor
        self.broker = broker
        self.execution_service = execution_service
        self.risk_guard = risk_guard or RiskGuard()
        self.run_repository = run_repository
        self.position_repository = position_repository
        self.order_repository = order_repository
        self.research_repository = research_repository
        self.reconciliation_repository = reconciliation_repository
        self.reconciliation_engine = reconciliation_engine or ReconciliationEngine(
            self.risk_guard
        )
        self.health = health_service
        self.alerts = alert_service
        self.approval_gate = approval_gate
        self.ledger = ledger
        self.dataset_version = dataset_version
        self.max_staleness_days = max_staleness_days
        self.cash_for_allocation = float(cash_for_allocation)
        self._equity_peak: float | None = None

    # -- helpers -------------------------------------------------------------

    def _equity(self, prices: Mapping[str, float]) -> float:
        total = self.broker.get_cash()
        for position in self.broker.get_positions():
            price = prices.get(position.symbol, position.average_price or 0.0)
            total += position.quantity * float(price)
        return total

    def _risk_context(
        self,
        prices: Mapping[str, float],
        data_last_updated: datetime,
        locked: bool,
    ) -> RiskContext:
        exposure: dict[str, float] = {}
        gross = 0.0
        for position in self.broker.get_positions():
            notional = position.quantity * float(prices.get(position.symbol, 0.0))
            if notional > 0:
                exposure[position.symbol] = notional
                gross += notional
        equity = self._equity(prices)
        self._equity_peak = (
            equity if self._equity_peak is None else max(self._equity_peak, equity)
        )
        return RiskContext(
            now=self.broker.clock
            if self.broker.clock.tzinfo
            else self.broker.clock.replace(tzinfo=UTC),
            equity_now=equity,
            equity_day_start=equity,
            equity_peak=self._equity_peak,
            position_exposure=exposure,
            gross_exposure=gross,
            data_last_updated=data_last_updated,
            broker_connected=True,
            order_timestamps=(),
            reconciliation_locked=locked,
        )

    def _reference_prices(self, accepted: pd.DataFrame) -> dict[str, float]:
        latest = (
            accepted.assign(_date=pd.to_datetime(accepted["date"]))
            .groupby("symbol")["_date"]
            .idxmax()
        )
        last_rows = accepted.loc[latest]
        return {
            str(row["symbol"]).upper(): float(row["close"])
            for _, row in last_rows.iterrows()
        }

    def _build_target(
        self,
        weights: pd.Series,
        prices: Mapping[str, float],
        as_of: date,
    ) -> PortfolioTarget:
        limits: dict[str, float] = {}
        targets: dict[str, int] = {}
        for symbol, weight in weights.items():
            symbol = str(symbol).upper()
            price = float(prices[symbol])
            limits[symbol] = price
            targets[symbol] = int(self.cash_for_allocation * float(weight) // price)
        return PortfolioTarget.model_validate(
            {
                "strategy_id": self.strategy.name,
                "hypothesis_id": self._hypothesis_id,
                "as_of": as_of,
                "limits": limits,
                "target_quantities": targets,
            }
        )

    @property
    def _hypothesis_id(self) -> str:
        return getattr(self.strategy, "hypothesis_id", "unassigned") or "unassigned"

    def _expected_state(self) -> tuple[dict[str, int], dict[str, str], dict[str, int]]:
        """Expected state, computed from the system's own persisted order
        ledger (NOT from the broker): this is what makes drift detectable."""
        from models.domain import OrderSide, OrderStatus

        positions: dict[str, int] = {}
        open_orders: dict[str, str] = {}
        filled: dict[str, int] = {}
        for intent in self.order_repository.list_intents():
            result = self.order_repository.get_result(intent.internal_order_id)
            if result is None:
                continue
            if result.status in (OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED):
                open_orders[result.internal_order_id] = result.symbol
            if (
                result.status
                in (
                    OrderStatus.FILLED,
                    OrderStatus.PARTIALLY_FILLED,
                )
                and result.filled_quantity > 0
            ):
                direction = 1 if result.side is OrderSide.BUY else -1
                positions[result.symbol] = (
                    positions.get(result.symbol, 0) + direction * result.filled_quantity
                )
                filled[result.internal_order_id] = result.filled_quantity
        return (
            {k: v for k, v in positions.items() if v},
            open_orders,
            filled,
        )

    # -- main flow -------------------------------------------------------------

    def run_day(
        self,
        run_id: str,
        raw_frame: pd.DataFrame,
        *,
        fundamentals: pd.DataFrame | None = None,
        approved_by: str | None = None,
    ) -> DailyRunResult:
        """Run the full daily flow for one day of data."""
        if approved_by:
            if isinstance(self.approval_gate, ManualApprovalGate):
                self.approval_gate.grant_approval(run_id)

        # 0) run claim: concurrent executions cannot duplicate the run.
        if not self.run_repository.claim_run(
            run_id, resume_awaiting_approval=bool(approved_by)
        ):
            self.alerts.warning("duplicate_run_rejected", run_id=run_id)
            return DailyRunResult(
                run_id=run_id,
                status="duplicate_run",
                health=self.health.state.value,
                risk_state="NOMINAL",
                approved=False,
            )

        # 1) data validation (fail closed on any quality issue).
        accepted, report = validate_market_bars(
            raw_frame, max_staleness_days=self.max_staleness_days
        )
        data_quality = report.to_dict()
        staleness = detect_data_staleness(
            accepted, max_staleness_days=self.max_staleness_days
        )
        if accepted.empty or report.issues:
            self.run_repository.save_run(run_id, "halted_data_quality", data_quality)
            self.health.set_state(
                SystemHealth.HALTED,
                f"data quality issues: {len(report.issues)}",
                run_id=run_id,
            )
            self.alerts.critical(
                "data_quality_halt",
                run_id=run_id,
                issue_count=len(report.issues),
                staleness=staleness.detail if staleness else None,
            )
            return DailyRunResult(
                run_id=run_id,
                status="halted_data_quality",
                health=self.health.state.value,
                risk_state="NOMINAL",
                approved=False,
                data_quality=data_quality,
            )

        data = MarketData.from_long_frame(accepted)
        prices = self._reference_prices(accepted)
        as_of = pd.Timestamp(accepted["date"].max()).date()
        data_last_updated = datetime.now(UTC)

        # 2) research: signals (never possible without validated data — step 1
        #    already halted on staleness/invalid rows).
        try:
            signals: Signal = self.strategy.generate_signals(data)
            weights = self.constructor.construct(signals, data)
        except Exception as exc:  # fail closed on any research failure
            self.run_repository.save_run(run_id, "halted_research", {"error": str(exc)})
            self.health.set_state(
                SystemHealth.HALTED, f"research failed: {exc}", run_id=run_id
            )
            self.alerts.critical("research_failed", run_id=run_id, error=str(exc))
            return DailyRunResult(
                run_id=run_id,
                status="halted_research",
                health=self.health.state.value,
                risk_state="NOMINAL",
                approved=False,
                signals_generated=False,
                data_quality=data_quality,
            )

        # 3) allocation (weights are indexed by Timestamp; select the as-of day).
        active = weights.loc[pd.Timestamp(as_of)]
        active = active[active > 0]
        if active.sum() <= 0:
            target = PortfolioTarget.model_validate(
                {
                    "strategy_id": self.strategy.name,
                    "hypothesis_id": self._hypothesis_id,
                    "as_of": as_of,
                    "limits": {},
                    "target_quantities": {},
                }
            )
        else:
            target = self._build_target(active, prices, as_of)

        # 4) risk checks before anything can be submitted.
        prior_reconciliation = self.reconciliation_repository.latest_result()
        prior_locked = bool(prior_reconciliation and prior_reconciliation.locked)
        context = self._risk_context(prices, data_last_updated, prior_locked)
        decision = self.risk_guard.evaluate(context)
        if decision.state is not RiskState.NOMINAL:
            self.run_repository.save_run(run_id, "halted_risk", decision.to_dict())
            self.health.set_state(
                SystemHealth.HALTED,
                f"risk state {decision.state.value}",
                run_id=run_id,
            )
            self.alerts.critical(
                "risk_halt",
                run_id=run_id,
                risk_state=decision.state.value,
                triggered_by=list(decision.triggered_by),
            )
            return DailyRunResult(
                run_id=run_id,
                status="halted_risk",
                health=self.health.state.value,
                risk_state=decision.state.value,
                approved=False,
                signals_generated=True,
                data_quality=data_quality,
            )

        # 5) HUMAN APPROVAL gate (fail closed).
        orders = []
        for symbol, quantity in sorted((target.target_quantities or {}).items()):
            price = target.limits.get(symbol)
            if price is not None and quantity > 0:
                orders.append(
                    {
                        "symbol": symbol,
                        "target_quantity": quantity,
                        "limit_price": price,
                    }
                )
        plan = ExecutionPlan(
            run_id=run_id,
            strategy_id=target.strategy_id,
            hypothesis_id=target.hypothesis_id,
            risk_state=decision.state.value,
            orders=tuple(orders),
            data_quality=data_quality,
        )
        approved = self.approval_gate.approve(run_id, plan)
        if not approved:
            self.run_repository.save_run(run_id, "awaiting_approval", plan.__dict__)
            self.alerts.info(
                "awaiting_human_approval", run_id=run_id, orders=len(plan.orders)
            )
            self._publish_status(run_id, decision, None, None)
            return DailyRunResult(
                run_id=run_id,
                status="awaiting_approval",
                health=self.health.state.value,
                risk_state=decision.state.value,
                approved=False,
                signals_generated=True,
                data_quality=data_quality,
            )

        # 6) paper execution.
        execution = self.execution_service.execute_targets(
            target,
            run_id=run_id,
            reference_prices=prices,
            risk_context=context,
            now=self.broker.clock,
        )
        if execution.halted:
            self.run_repository.save_run(run_id, "halted_risk", execution.to_dict())
            self.health.set_state(
                SystemHealth.HALTED,
                f"execution halted: risk state {execution.risk_state}",
                run_id=run_id,
            )
            self.alerts.critical(
                "execution_halted", run_id=run_id, risk_state=execution.risk_state
            )
            return DailyRunResult(
                run_id=run_id,
                status="halted_risk",
                health=self.health.state.value,
                risk_state=execution.risk_state,
                approved=True,
                execution=execution,
                signals_generated=True,
                data_quality=data_quality,
            )

        # 7) fill reconciliation.
        expected_positions, expected_open, expected_filled = self._expected_state()
        fill_input = ReconciliationInput(
            run_id=run_id,
            as_of=self.broker.clock,
            expected_positions=expected_positions,
            expected_open_orders=expected_open,
            expected_filled=expected_filled,
            actual_positions=self.broker.get_positions(),
            actual_orders=self._all_broker_orders(),
            actual_open_orders=self.broker.get_open_orders(),
        )
        fill_result = self.reconciliation_engine.reconcile(fill_input)
        self.reconciliation_engine.persist(fill_result, self.reconciliation_repository)

        # 8) EOD reconciliation (same engine, after any pending-order
        #    expiry). Legitimately pending orders the system knows about are
        #    expected; only unknown/mismatched state locks the account.
        eod_input = ReconciliationInput(
            run_id=f"{run_id}:eod",
            as_of=self.broker.clock,
            expected_positions=expected_positions,
            expected_open_orders=expected_open,
            expected_filled=expected_filled,
            actual_positions=self.broker.get_positions(),
            actual_orders=self._all_broker_orders(),
            actual_open_orders=self.broker.get_open_orders(),
        )
        eod_result = self.reconciliation_engine.reconcile(eod_input)
        self.reconciliation_engine.persist(eod_result, self.reconciliation_repository)

        reconciliation = eod_result
        if not reconciliation.matched:
            self.run_repository.save_run(
                run_id,
                "locked_reconciliation",
                reconciliation.model_dump(mode="json"),
            )
            self.health.set_state(
                SystemHealth.LOCKED,
                "reconciliation mismatch",
                run_id=run_id,
                mismatches=[m.kind for m in reconciliation.mismatches],
            )
            self.alerts.critical(
                "reconciliation_lock",
                run_id=run_id,
                kinds=[m.kind for m in reconciliation.mismatches],
            )
            self._persist_research(run_id, status="halted", execution=execution)
            return DailyRunResult(
                run_id=run_id,
                status="locked_reconciliation",
                health=self.health.state.value,
                risk_state="LOCK_ACCOUNT",
                approved=True,
                execution=execution,
                reconciliation=reconciliation,
                signals_generated=True,
                data_quality=data_quality,
            )

        # 9) healthy completion.
        self.run_repository.save_run(run_id, "completed", execution.to_dict())
        self.health.set_state(
            SystemHealth.HEALTHY, "daily run completed", run_id=run_id
        )
        self.alerts.info(
            "daily_run_completed", run_id=run_id, orders=len(execution.submitted)
        )
        self._publish_status(run_id, decision, execution, reconciliation)
        self._persist_research(run_id, status="accepted", execution=execution)
        return DailyRunResult(
            run_id=run_id,
            status="completed",
            health=self.health.state.value,
            risk_state=decision.state.value,
            approved=True,
            execution=execution,
            reconciliation=reconciliation,
            signals_generated=True,
            data_quality=data_quality,
        )

    def _all_broker_orders(self) -> list:
        """All orders known to the broker (open + terminal)."""
        results = []
        for intent in self.order_repository.list_intents():
            result = self.order_repository.get_result(intent.internal_order_id)
            if result is not None:
                results.append(result)
        return results

    def _persist_research(
        self,
        run_id: str,
        *,
        status: str,
        execution: ExecutionSummary | None,
    ) -> None:
        result = ResearchResult.model_validate(
            {
                "hypothesis_id": self._hypothesis_id,
                "strategy_id": self.strategy.name,
                "status": status,
                "metrics": {
                    "orders_submitted": len(execution.submitted) if execution else 0,
                    "orders_skipped": len(execution.skipped) if execution else 0,
                },
                "dataset_version": self.dataset_version,
                "run_id": run_id,
                "created_at": datetime.now(UTC),
            }
        )
        self.research_repository.save_result(result)
        if self.ledger is not None:
            self.ledger.for_experiment(
                _ledger_experiment(self.strategy, self._hypothesis_id),
                status=status,
                metrics=result.metrics,
                dataset_version=self.dataset_version,
            )

    def _publish_status(
        self,
        run_id: str,
        decision: Any,
        execution: ExecutionSummary | None,
        reconciliation: ReconciliationResult | None,
    ) -> None:
        state = self.broker.get_state()
        self.health.write_extended_status(
            {
                "latest_run": run_id,
                "risk_state": decision.state.value,
                "risk_triggered_by": list(decision.triggered_by),
                "reconciliation": (
                    {
                        "matched": reconciliation.matched,
                        "locked": reconciliation.locked,
                    }
                    if reconciliation
                    else "not run"
                ),
                "paper_cash": state["cash"],
                "paper_positions": state["positions"],
                "open_orders": state["open_orders"],
                "execution": execution.to_dict() if execution else None,
                "alerts_recent": [
                    alert.to_dict() for alert in self.alerts.list_alerts()[-10:]
                ],
            }
        )


def _ledger_experiment(strategy: Strategy, hypothesis_id: str) -> Any:
    """Build a minimal Experiment for ledger recording from the strategy."""
    from datetime import datetime as _dt

    from research.contracts import Experiment

    return Experiment(
        hypothesis_id=hypothesis_id or "unassigned",
        strategy=strategy.name,
        parameters=dict(getattr(strategy, "parameters", {}) or {}),
        factor_set=["n/a"],
        universe="pipeline",
        created_at=_dt.now(UTC),
    )
