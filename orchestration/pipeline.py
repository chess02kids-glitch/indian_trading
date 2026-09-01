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

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Mapping, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import pandas as pd

from data.quality import classify_issues, detect_data_staleness, validate_market_bars
from execution.paper import PaperBroker
from execution.service import ExecutionService, ExecutionSummary
from models.domain import (
    PortfolioTarget,
    ReconciliationResult,
    ResearchResult,
)
from observability.alerts import AlertService
from observability.health import HealthService, SystemHealth
from reconciliation.engine import (
    ReconciliationEngine,
    ReconciliationError,
    ReconciliationInput,
)
from research.contracts import MarketData, Signal, Strategy
from research.ledger import HypothesisLedger
from risk_kill import RiskContext, RiskGuard, RiskState
from store.memory import InMemoryEquityRepository
from store.protocols import (
    EquityRepository,
    EquitySnapshot,
    OrderRepository,
    PositionRepository,
    ReconciliationRepository,
    ResearchRepository,
    RunRepository,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalGate",
    "DailyPipeline",
    "DailyRunResult",
    "ExecutionPlan",
    "ManualApprovalGate",
    "RecordingApprovalGate",
]


#: Indian market timezone. Session dates (and therefore the as-of bound for the
#: look-ahead guard) are IST dates, not UTC dates: after 00:00 IST but before
#: 18:30 UTC the two still differ by a day.
IST = ZoneInfo("Asia/Kolkata")


def _ist_today() -> date:
    """Return the current calendar date in IST."""
    return datetime.now(IST).date()


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
        equity_repository: EquityRepository | None = None,
        reconciliation_engine: ReconciliationEngine | None = None,
        health_service: HealthService,
        alert_service: AlertService,
        approval_gate: ApprovalGate,
        ledger: HypothesisLedger | None = None,
        dataset_version: str = "unknown",
        max_staleness_days: float = 6.0,
        cash_for_allocation: float = 1_000_000.0,
        max_advisory_issue_fraction: float = 0.05,
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
        # AUDIT-030: mark-to-market history. Defaults to an in-memory store so
        # existing callers keep working, but a real deployment must pass the
        # SQLite/Supabase repository or the daily-loss and drawdown checks
        # reset on every restart.
        self.equity_repository: EquityRepository = (
            equity_repository or InMemoryEquityRepository()
        )
        self._incomplete_context: list[str] = []
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
        # AUDIT-010: advisory data-quality issues are tolerated up to this
        # fraction of (symbols x sessions); beyond it the day halts.
        if not 0.0 <= max_advisory_issue_fraction <= 1.0:
            raise ValueError("max_advisory_issue_fraction must be within [0, 1]")
        self.max_advisory_issue_fraction = float(max_advisory_issue_fraction)
        self._equity_peak: float | None = None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _restore_risk_state() -> str | None:
        """AUDIT-021: the protective state the previous process died with.

        Returns ``None`` when nothing protective was recorded, so a normal
        start is unaffected. Never raises.
        """
        try:
            from datahub.kill_switch import restore_risk_state

            value = restore_risk_state()
        except Exception:  # noqa: BLE001
            logger.exception("risk_state_restore_failed")
            return None
        if not value:
            return None
        # Only the protective states survive; NOMINAL must never be restored
        # (that would be the same bug in the other direction).
        protective = {
            member.value
            for member in RiskState
            if member is not RiskState.NOMINAL
        }
        return value if value in protective else None

    def _equity(self, prices: Mapping[str, float]) -> float:
        total = self.broker.get_cash()
        for position in self.broker.get_positions():
            price = prices.get(position.symbol, position.average_price or 0.0)
            total += position.quantity * float(price)
        return total

    def _mark_to_market(self, prices: Mapping[str, float]) -> EquitySnapshot:
        """Record one equity observation and return it.

        AUDIT-030: called on every mark-to-market so ``equity_day_start`` and
        ``equity_peak`` come from persisted history rather than from whatever
        the current process happens to remember.
        """
        cash = float(self.broker.get_cash())
        market_value = 0.0
        for position in self.broker.get_positions():
            market_value += position.quantity * float(
                prices.get(position.symbol, position.average_price or 0.0)
            )
        snapshot = EquitySnapshot(
            date=_ist_today().isoformat(),
            equity=cash + market_value,
            cash=cash,
            market_value=market_value,
            recorded_at=datetime.now(UTC),
        )
        try:
            self.equity_repository.save_snapshot(snapshot)
        except Exception:  # noqa: BLE001 - persistence must never break a halt
            logger.exception("equity_snapshot_persist_failed")
        return snapshot

    def _broker_connected(self) -> bool | None:
        """Probe the broker; ``None`` means "unknown", which fails closed.

        AUDIT-030: this used to be the literal ``True``, so
        ``RiskGuard.check_broker_connectivity`` could never fire. A broker that
        cannot be probed is not a connected broker, so ``None`` is returned
        (and the guard maps that to ``LOCK_ACCOUNT``) rather than ``True``.
        """
        probe = getattr(self.broker, "ping", None)
        if not callable(probe):
            self._note_incomplete_context("broker_connected", "broker has no ping()")
            return None
        try:
            connected = bool(probe())
        except Exception:  # noqa: BLE001 - an unprobed broker is not connected
            logger.exception("broker_ping_failed")
            self._note_incomplete_context("broker_connected", "ping raised")
            return None
        try:
            from datahub.state import beat

            beat(
                "broker_ping",
                {"connected": connected, "broker": type(self.broker).__name__},
            )
        except Exception:  # noqa: BLE001 - heartbeats are best-effort
            logger.debug("broker_ping_heartbeat_failed", exc_info=True)
        return connected

    def _order_timestamps(self, now: datetime) -> tuple[datetime, ...]:
        """Timestamps of orders actually submitted in the last hour.

        AUDIT-030: this used to be the empty tuple, so
        ``RiskGuard.check_order_rate`` could never fire no matter how fast the
        system submitted orders.
        """
        timestamps: list[datetime] = []
        try:
            window_start = now.timestamp() - 3600.0
            for intent in self.order_repository.list_intents():
                result = self.order_repository.get_result(intent.internal_order_id)
                candidate = getattr(result, "timestamp", None) or intent.timestamp
                if candidate is None:
                    continue
                moment = candidate if candidate.tzinfo else candidate.replace(tzinfo=UTC)
                if moment.timestamp() >= window_start:
                    timestamps.append(moment)
        except Exception:  # noqa: BLE001 - an unreadable ledger is not an empty one
            logger.exception("order_timestamps_read_failed")
            self._note_incomplete_context("order_timestamps", "order ledger unreadable")
            return ()
        return tuple(sorted(timestamps))

    def _note_incomplete_context(self, field_name: str, reason: str) -> None:
        """Log — and surface — a risk input that could not be determined."""
        self._incomplete_context.append(f"{field_name}: {reason}")
        logger.warning("risk_context_incomplete field=%s reason=%s", field_name, reason)
        if self.alerts is not None:
            try:
                self.alerts.warning(
                    "risk_context_incomplete",
                    field=field_name,
                    reason=reason,
                )
            except Exception:  # noqa: BLE001
                logger.debug("risk_context_incomplete_alert_failed", exc_info=True)

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

        # The day's opening equity: the first mark-to-market recorded today.
        # That is the honest definition of "start of day" for a system that is
        # started once per session — and it means a re-run or a restart inside
        # the same day measures the loss against the real opening value, not
        # against itself.
        today = _ist_today().isoformat()
        opening = None
        try:
            opening = self.equity_repository.snapshot_for_date(today)
        except Exception:  # noqa: BLE001
            logger.exception("equity_snapshot_read_failed")
        if opening is None:
            opening = self._mark_to_market(prices)
        equity_day_start: float | None = float(opening.equity)

        # The peak must survive a restart, so it is the max over the persisted
        # history *and* the in-process high-water mark.
        peak_candidates = [equity]
        try:
            peak_candidates.extend(
                float(row.equity) for row in self.equity_repository.history()
            )
        except Exception:  # noqa: BLE001
            logger.exception("equity_history_read_failed")
            self._note_incomplete_context("equity_peak", "equity history unreadable")
        self._equity_peak = max(self._equity_peak or equity, *peak_candidates)

        now = (
            self.broker.clock
            if self.broker.clock.tzinfo
            else self.broker.clock.replace(tzinfo=UTC)
        )
        return RiskContext(
            now=now,
            equity_now=equity,
            equity_day_start=equity_day_start,
            equity_peak=self._equity_peak,
            position_exposure=exposure,
            gross_exposure=gross,
            data_last_updated=data_last_updated,
            broker_connected=self._broker_connected(),
            order_timestamps=self._order_timestamps(now),
            reconciliation_locked=locked,
        )

    def _advisory_budget(self, accepted: pd.DataFrame) -> int:
        """How many advisory issues a run may carry before it halts.

        AUDIT-010 compensating control #1: the relaxation is bounded. Without
        a cap, "advisory" would eventually mean "ignored".
        """
        if accepted.empty:
            return 0
        sessions = int(accepted["date"].nunique())
        symbols = int(accepted["symbol"].nunique())
        return max(1, int(symbols * sessions * self.max_advisory_issue_fraction))

    @staticmethod
    def _record_risk_state(state: RiskState, reason: str = "") -> None:
        """AUDIT-021: persist the automatic protective state across restarts.

        ``RiskGuard`` holds no state of its own, so without this a
        ``LOCK_ACCOUNT`` decision was lost the moment the process exited and
        the next run began from ``NOMINAL``.
        """
        try:
            from datahub.kill_switch import record_risk_state

            record_risk_state(state.value, reason=reason)
        except Exception:  # noqa: BLE001 - never let persistence break a halt
            logger.exception("risk_state_persist_failed state=%s", state.value)

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
        as_of: date | None = None,
    ) -> DailyRunResult:
        """Run the full daily flow for one day of data.

        ``as_of`` bounds the look-ahead guard: bars dated strictly after it are
        reported as ``future_date`` and excluded, and (because the pipeline
        halts on any issue) they halt the day. It defaults to *today in IST* —
        never to the maximum date in the frame, which would make the guard a
        no-op. Pass an explicit date for deterministic replay of past sessions.
        """
        if approved_by:
            if isinstance(self.approval_gate, ManualApprovalGate):
                self.approval_gate.grant_approval(run_id)

        # 0a) AUDIT-021 — operator kill switch, checked before the run claim so
        #     that an armed switch does not even consume the run id. datahub.
        #     state is the single authority and is persisted, so this also holds
        #     across a restart. Fails closed: an unreadable state file stops the
        #     day rather than permitting it.
        try:
            from datahub.kill_switch import blocked_reason, require_not_killed

            kill_switch_engaged = require_not_killed("run_day")
            kill_switch_detail = blocked_reason()
        except Exception:  # noqa: BLE001 - never let a guard raise into trading
            logger.exception("kill_switch_lookup_failed_failing_closed")
            kill_switch_engaged, kill_switch_detail = True, "kill switch unreadable"
        if kill_switch_engaged:
            self.health.set_state(
                SystemHealth.LOCKED,
                "operator kill switch is armed",
                run_id=run_id,
            )
            self.alerts.critical(
                "kill_switch_halt", run_id=run_id, detail=kill_switch_detail
            )
            return DailyRunResult(
                run_id=run_id,
                status="halted_kill_switch",
                health=self.health.state.value,
                risk_state=RiskState.LOCK_ACCOUNT.value,
                approved=False,
                metrics={"halt_reason": kill_switch_detail},
            )

        # 0b) AUDIT-021 — re-apply the last automatic protective state.
        #     RiskGuard is in-memory only, so before this change a process that
        #     locked the account was forgotten on restart and the next run
        #     started from NOMINAL.
        restored = self._restore_risk_state()
        if restored is not None:
            self.health.set_state(
                SystemHealth.LOCKED,
                f"restored protective risk state {restored}",
                run_id=run_id,
            )
            self.alerts.critical(
                "restored_protective_risk_state",
                run_id=run_id,
                risk_state=restored,
            )
            return DailyRunResult(
                run_id=run_id,
                status="halted_risk",
                health=self.health.state.value,
                risk_state=restored,
                approved=False,
                metrics={"halt_reason": f"restored protective state {restored}"},
            )

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
        #    AUDIT-006: forward the as-of bound so the look-ahead guard in
        #    data.quality actually runs. Defaulting to the frame's own max date
        #    would let a frame containing future bars validate itself.
        effective_as_of = as_of if as_of is not None else _ist_today()
        accepted, report = validate_market_bars(
            raw_frame,
            max_staleness_days=self.max_staleness_days,
            as_of=effective_as_of,
        )
        staleness = detect_data_staleness(
            accepted, max_staleness_days=self.max_staleness_days
        )
        # AUDIT-010: not every quality issue is a reason to stop the day.
        # ``missing_candle`` and ``off_calendar`` describe the shape of real
        # NSE data (symbols that did not trade, Budget special sessions) and
        # reject no rows; everything else means the data is wrong. An
        # unrecognised kind is treated as blocking.
        blocking, advisory = classify_issues(report.issues)
        budget = self._advisory_budget(accepted)
        over_budget = len(advisory) > budget
        data_quality = report.to_dict()
        data_quality.update(
            {
                "blocking_issue_count": len(blocking),
                "blocking_issue_kinds": sorted({issue.kind for issue in blocking}),
                "advisory_issue_count": len(advisory),
                "advisory_issue_kinds": sorted({issue.kind for issue in advisory}),
                "advisory_budget": budget,
                "advisory_over_budget": over_budget,
            }
        )
        if advisory:
            # A degraded run must never be silent (compensating control #2).
            self.alerts.warning(
                "data_quality_advisory",
                run_id=run_id,
                advisory_issue_count=len(advisory),
                advisory_issue_kinds=sorted({issue.kind for issue in advisory}),
                budget=budget,
                over_budget=over_budget,
            )
        if accepted.empty or blocking or over_budget:
            reason = (
                "no rows accepted by data validation"
                if accepted.empty
                else (
                    f"advisory issues {len(advisory)} exceed budget {budget}"
                    if over_budget and not blocking
                    else f"blocking data quality issues: {len(blocking)}"
                )
            )
            self.run_repository.save_run(run_id, "halted_data_quality", data_quality)
            self.health.set_state(
                SystemHealth.HALTED,
                reason,
                run_id=run_id,
            )
            self.alerts.critical(
                "data_quality_halt",
                run_id=run_id,
                reason=reason,
                blocking_issue_count=len(blocking),
                advisory_issue_count=len(advisory),
                advisory_budget=budget,
                staleness=staleness.detail if staleness else None,
            )
            return DailyRunResult(
                run_id=run_id,
                status="halted_data_quality",
                health=self.health.state.value,
                risk_state="NOMINAL",
                approved=False,
                data_quality=data_quality,
                metrics={"halt_reason": reason},
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
        self._incomplete_context = []
        context = self._risk_context(prices, data_last_updated, prior_locked)
        decision = self.risk_guard.evaluate(context)
        self._record_risk_state(decision.state)
        if decision.state is not RiskState.NOMINAL:
            self.run_repository.save_run(
                run_id,
                "halted_risk",
                {
                    **decision.to_dict(),
                    "incomplete_context": list(self._incomplete_context),
                },
            )
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
            self._record_risk_state(RiskState.LOCK_ACCOUNT, "reconciliation mismatch")
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
            metrics=(
                {"risk_context_incomplete": list(self._incomplete_context)}
                if self._incomplete_context
                else {}
            ),
        )

    def _all_broker_orders(self) -> list:
        """All orders as **the broker** reports them (open + terminal).

        AUDIT-022: this used to read ``self.order_repository`` — the same
        store that :meth:`_expected_state` reads. Every order-side check was
        therefore comparing the local ledger with itself: ``_check_fills``,
        ``_check_duplicates`` and the order half of ``_check_open_orders``
        could not fail, no matter what the broker actually did.

        Orders the local store never submitted (no persisted result — for
        example an intent that was skipped for want of a reference price) are
        not expected at the broker and are skipped. Anything the local store
        *believes* was submitted must be enumerable by the broker; if it is
        not, the broker's view is unknown and reconciliation fails closed
        with :class:`ReconciliationError` instead of silently substituting
        the stored result.
        """
        orders: list[Any] = []
        for intent in self.order_repository.list_intents():
            local = self.order_repository.get_result(intent.internal_order_id)
            if local is None:
                # Never submitted: the broker legitimately has nothing.
                continue
            try:
                record = self.broker.get_order_status(intent.internal_order_id)
            except Exception as exc:  # noqa: BLE001 - an unprobed broker is unknown
                raise ReconciliationError(
                    f"broker could not be queried for order "
                    f"{intent.internal_order_id}: {type(exc).__name__}: {exc}"
                ) from exc
            if record is None:
                raise ReconciliationError(
                    f"order {intent.internal_order_id} was submitted according "
                    "to the local ledger but the broker cannot enumerate it; "
                    "reconciliation cannot be performed"
                )
            orders.append(self._as_order_result(record, intent.internal_order_id))
        return orders

    @staticmethod
    def _as_order_result(record: Any, internal_order_id: str) -> Any:
        """Normalise a broker status record to :class:`OrderResult`."""
        from models.domain import OrderResult

        if isinstance(record, OrderResult):
            return record
        # A real adapter returns its own BrokerOrderRecord.
        from broker.reconciler import record_to_result

        result = record_to_result(record)
        if result.internal_order_id != internal_order_id:
            result = result.model_copy(update={"internal_order_id": internal_order_id})
        return result

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
