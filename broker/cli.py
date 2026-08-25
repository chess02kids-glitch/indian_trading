"""Operator CLI for the broker sandbox layer.

Commands (also reachable via ``python main.py broker ...``)::

    broker health      [--broker upstox|dhan|all]
    broker login       <broker> [--code CODE]
    broker funds       <broker>
    broker holdings    <broker>
    broker positions   <broker>
    broker orders      <broker>            (recent sandbox orders, read-only)
    broker sandbox-order <broker> --symbol X --side BUY --quantity N --limit-price P
    broker sandbox-cancel <broker> --internal-id ID
    broker reconcile   <broker>

Safety contract:

* ``QUANT_EXECUTION_MODE`` is the feature flag. Unset defaults to ``SANDBOX``
  for this CLI (the sandbox is the only broker this build can reach).
  ``LIVE`` refuses every command with exit code 3. Mutating commands
  (``sandbox-order``/``sandbox-cancel``) additionally require ``SANDBOX``.
* ``sandbox-order`` runs the full chain: mode gate → OrderIntent validation
  (LIMIT-only) → risk-kill guard → duplicate prevention → rate limiter →
  token gate → sandbox broker, then persists a status document.
* Login is manual: ``broker login <broker>`` prints a URL; the operator
  obtains a code and re-runs with ``--code``. Nothing is automated.

Exit codes: 0 ok · 1 usage/runtime error · 2 order rejected or unknown ·
3 refused (mode, risk guard, or reconciliation lock).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Callable, Mapping

from broker.adapter import SUPPORTED_BROKERS, BaseSandboxAdapter, create_adapter
from broker.errors import (
    BrokerError,
    LiveTradingDisabledError,
    SandboxOnlyError,
)
from broker.mode import OperatingMode, resolve_operating_mode
from broker.reconciler import SandboxReconciler
from broker.safe_execution import SandboxExecutionAdapter
from broker.status import (
    collect_broker_health,
    load_prior_status,
    merge_recent_orders,
    order_entry,
    summarize_reconciliation,
    write_broker_status,
)
from execution.idempotency import compute_idempotency_key
from models.domain import (
    OrderIntent,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
)
from store.memory import InMemoryOrderRepository, InMemoryPositionRepository

__all__ = [
    "EXIT_OK",
    "EXIT_ERROR",
    "EXIT_REJECTED",
    "EXIT_REFUSED",
    "build_parser",
    "cli_main",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REJECTED = 2
EXIT_REFUSED = 3

AdapterFactory = Callable[[str], BaseSandboxAdapter]
Printer = Callable[[str], None]


def _default_factory(broker: str) -> BaseSandboxAdapter:
    return create_adapter(broker)


def _adapters(selection: str, factory: AdapterFactory) -> dict[str, BaseSandboxAdapter]:
    names = list(SUPPORTED_BROKERS) if selection == "all" else [selection]
    return {name: factory(name) for name in names}


def _resolve_cli_mode(environ: Mapping[str, str] | None) -> OperatingMode:
    """Resolve the feature flag; CLI default is SANDBOX (its only broker)."""
    return resolve_operating_mode(environ, default=OperatingMode.SANDBOX)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="broker",
        description="Broker sandbox operations (sandbox-only; LIVE disabled)",
    )
    sub = parser.add_subparsers(dest="broker_command")

    health = sub.add_parser("health", help="broker connectivity/token/sandbox health")
    health.add_argument("--broker", default="all", choices=[*SUPPORTED_BROKERS, "all"])

    login = sub.add_parser("login", help="manual sandbox login (OAuth skeleton)")
    login.add_argument("broker", choices=list(SUPPORTED_BROKERS))
    login.add_argument(
        "--code", default=None, help="authorization code from the broker page"
    )
    login.add_argument("--state", default="cli-login", help="OAuth state value")

    for name in ("funds", "holdings", "positions"):
        cmd = sub.add_parser(name, help=f"show {name} (read-only)")
        cmd.add_argument("broker", choices=list(SUPPORTED_BROKERS))

    orders = sub.add_parser("orders", help="recent sandbox orders (read-only)")
    orders.add_argument("broker", choices=list(SUPPORTED_BROKERS))

    place = sub.add_parser(
        "sandbox-order", help="place one LIMIT order against the sandbox broker"
    )
    place.add_argument("broker", choices=list(SUPPORTED_BROKERS))
    place.add_argument("--symbol", required=True)
    place.add_argument("--side", required=True, choices=["BUY", "SELL"])
    place.add_argument("--quantity", required=True, type=int)
    place.add_argument("--limit-price", required=True, type=float)
    place.add_argument("--reference-price", type=float, default=None)
    place.add_argument("--strategy-id", default="sandbox-manual")
    place.add_argument("--hypothesis-id", default="sandbox-manual")
    place.add_argument("--rebalance-date", default=None, help="YYYY-MM-DD")

    cancel = sub.add_parser("sandbox-cancel", help="cancel an open sandbox order")
    cancel.add_argument("broker", choices=list(SUPPORTED_BROKERS))
    cancel.add_argument("--internal-id", required=True)

    recon = sub.add_parser("reconcile", help="end-of-day sandbox reconciliation")
    recon.add_argument("broker", choices=list(SUPPORTED_BROKERS))

    return parser


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _print_json(out: Printer, payload: Any) -> None:
    out(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _persist_status(
    environ: Mapping[str, str] | None,
    adapters: Mapping[str, BaseSandboxAdapter],
    *,
    new_orders: Sequence[Mapping[str, Any]] = (),
    reconciliation_health: Any = None,
) -> dict[str, Any]:
    """Merge new orders into the status document and persist it."""
    prior = load_prior_status(environ)
    recent = merge_recent_orders(
        prior.get("recent_sandbox_orders") or [], list(new_orders)
    )
    if reconciliation_health is None:
        reconciliation_health = prior.get("reconciliation_health", "unknown")
    document = collect_broker_health(
        adapters,
        reconciliation_health=reconciliation_health,
        recent_sandbox_orders=recent,
    )
    write_broker_status(document, environ)
    return document


def _risk_context(adapter: BaseSandboxAdapter, now: datetime) -> Any:
    """Build the sandbox risk context from live broker/account reads."""
    from risk_kill import RiskContext

    funds = adapter.get_funds()
    equity = funds.available_cash
    exposure: dict[str, float] = {}
    gross = 0.0
    for position in adapter.get_positions():
        if not position.quantity:
            continue
        quote = adapter.get_quote(position.symbol, position.exchange)
        notional = position.quantity * quote.last_price
        exposure[position.symbol] = notional
        gross += notional
    return RiskContext(
        now=now,
        equity_now=max(equity, 1e-9),
        equity_day_start=max(equity, 1e-9),
        equity_peak=max(equity + gross, 1e-9),
        position_exposure=exposure,
        gross_exposure=gross,
        data_last_updated=now,
        broker_connected=adapter.ping(),
        order_timestamps=(),
        reconciliation_locked=False,
    )


def _parse_rebalance_date(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(UTC).date()


def _intent_from_args(args: argparse.Namespace, now: datetime) -> OrderIntent:
    rebalance = _parse_rebalance_date(args.rebalance_date)
    side = OrderSide(args.side)
    key = compute_idempotency_key(
        {
            "strategy_id": args.strategy_id,
            "hypothesis_id": args.hypothesis_id,
            "symbol": args.symbol,
            "side": side.value,
            "quantity": args.quantity,
            "limit_price": args.limit_price,
            "order_type": OrderType.LIMIT.value,
            "rebalance_date": rebalance.isoformat(),
        }
    )
    internal = (
        "cli-" + hashlib.sha256(f"{key}|{args.broker}".encode("utf-8")).hexdigest()[:16]
    )
    return OrderIntent.model_validate(
        {
            "internal_order_id": internal,
            "idempotency_key": key,
            "strategy_id": args.strategy_id,
            "hypothesis_id": args.hypothesis_id,
            "symbol": args.symbol,
            "exchange": "NSE",
            "side": side,
            "quantity": args.quantity,
            "limit_price": args.limit_price,
            "order_type": OrderType.LIMIT,
            "timestamp": now,
        }
    )


def _require_sandbox_mode(mode: OperatingMode) -> None:
    if mode is OperatingMode.LIVE:
        raise LiveTradingDisabledError(
            "LIVE mode refuses all broker operations; live execution is disabled"
        )
    if mode is not OperatingMode.SANDBOX:
        raise SandboxOnlyError(
            f"mode {mode.value} does not permit sandbox orders; "
            "set QUANT_EXECUTION_MODE=SANDBOX"
        )


def _require_not_live(mode: OperatingMode) -> None:
    if mode is OperatingMode.LIVE:
        raise LiveTradingDisabledError(
            "LIVE mode refuses all broker operations; live execution is disabled"
        )


# --------------------------------------------------------------------------
# command handlers
# --------------------------------------------------------------------------


def _handle_health(
    args: argparse.Namespace,
    factory: AdapterFactory,
    environ: Mapping[str, str] | None,
    out: Printer,
) -> int:
    adapters = _adapters(args.broker, factory)
    document = _persist_status(environ, adapters)
    _print_json(out, document)
    return EXIT_OK


def _handle_login(
    args: argparse.Namespace,
    factory: AdapterFactory,
    environ: Mapping[str, str] | None,
    out: Printer,
) -> int:
    adapter = factory(args.broker)
    if not args.code:
        url = adapter.login_url(args.state)
        out(f"--- {adapter.broker_name} sandbox login ---")
        out("Visit the sandbox authorization URL (manual step):")
        out(url)
        out(f"Then re-run: broker login {adapter.broker_name} --code <CODE>")
        return EXIT_OK
    try:
        record = adapter.complete_login(args.code)
    except BrokerError as exc:
        out(f"login failed: {exc}")
        return EXIT_ERROR
    from broker.token import mask_token

    status = adapter.token_manager.status(args.broker)
    _print_json(
        out,
        {
            "broker": record.broker,
            "authenticated": True,
            "masked_token": mask_token(record.access_token),
            "issued_at": record.issued_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "token_state": status.state,
        },
    )
    _persist_status(environ, {args.broker: adapter})
    return EXIT_OK


def _handle_read(
    command: str,
    args: argparse.Namespace,
    factory: AdapterFactory,
    environ: Mapping[str, str] | None,
    out: Printer,
) -> int:
    adapter = factory(args.broker)
    try:
        if command == "funds":
            payload: Any = adapter.get_funds().model_dump(mode="json")
        elif command == "holdings":
            payload = [
                holding.model_dump(mode="json") for holding in adapter.get_holdings()
            ]
        elif command == "positions":
            payload = [
                position.model_dump(mode="json") for position in adapter.get_positions()
            ]
        elif command == "orders":
            prior = load_prior_status(environ)
            payload = {
                "recent_sandbox_orders": prior.get("recent_sandbox_orders", []),
                "authenticated": adapter.is_authenticated(),
            }
        else:  # pragma: no cover - argparse prevents
            raise BrokerError(f"unknown read command {command!r}")
    except BrokerError as exc:
        out(f"{command} failed: {exc}")
        return EXIT_ERROR
    _print_json(out, payload)
    return EXIT_OK


def _handle_sandbox_order(
    args: argparse.Namespace,
    factory: AdapterFactory,
    environ: Mapping[str, str] | None,
    out: Printer,
) -> int:
    mode = _resolve_cli_mode(environ)
    _require_sandbox_mode(mode)
    adapter = factory(args.broker)
    now = datetime.now(UTC)

    # 1. risk-kill guard is in the path: a protective state refuses the order.
    from risk_kill import RiskGuard, RiskState

    try:
        context = _risk_context(adapter, now)
    except BrokerError as exc:
        out(f"cannot build risk context: {exc}")
        return EXIT_ERROR
    decision = RiskGuard().evaluate(context)
    if decision.state is not RiskState.NOMINAL:
        _print_json(
            out,
            {
                "refused": True,
                "reason": f"risk guard state {decision.state.value}",
                "triggered_by": list(decision.triggered_by),
            },
        )
        return EXIT_REFUSED

    # 2. intent construction + full safe-execution chain.
    try:
        intent = _intent_from_args(args, now)
    except (ValueError, KeyError) as exc:
        out(f"invalid order: {exc}")
        return EXIT_ERROR

    reference = args.reference_price
    if reference is None:
        try:
            reference = adapter.get_quote(intent.symbol, intent.exchange).last_price
        except BrokerError as exc:
            out(f"cannot fetch reference price: {exc}")
            return EXIT_ERROR

    executor = SandboxExecutionAdapter(adapter)
    try:
        result = executor.submit_order(intent, reference)
    except (BrokerError, ValueError) as exc:
        _print_json(out, {"refused": True, "reason": str(exc)})
        return EXIT_REFUSED

    _persist_status(environ, {args.broker: adapter}, new_orders=[order_entry(result)])
    _print_json(
        out,
        {
            "mode": mode.value,
            "decision": decision.state.value,
            "order": order_entry(result),
        },
    )

    if result.status in (
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING,
    ):
        return EXIT_OK
    return EXIT_REJECTED


def _handle_sandbox_cancel(
    args: argparse.Namespace,
    factory: AdapterFactory,
    environ: Mapping[str, str] | None,
    out: Printer,
) -> int:
    mode = _resolve_cli_mode(environ)
    _require_sandbox_mode(mode)
    adapter = factory(args.broker)
    executor = SandboxExecutionAdapter(adapter)
    # Resolve the internal id to a broker order id / tag via the status doc
    # when possible (internal ids are not stored broker-side).
    ref = args.internal_id
    for entry in load_prior_status(environ).get("recent_sandbox_orders") or []:
        if str(entry.get("internal_order_id")) == ref and entry.get("broker_order_id"):
            ref = str(entry["broker_order_id"])
            break
    result = executor.cancel_order(ref)
    if result is None:
        out(f"unknown order {args.internal_id!r}")
        return EXIT_ERROR
    _print_json(out, {"order": order_entry(result)})
    if result.status is OrderStatus.CANCELLED:
        return EXIT_OK
    return EXIT_REJECTED


def _recorded_expectations(
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive expected state from previously recorded sandbox orders."""
    positions: dict[str, int] = {}
    open_orders: dict[str, str] = {}
    filled: dict[str, int] = {}
    for entry in entries:
        internal = str(entry.get("internal_order_id", ""))
        if not internal:
            continue
        status = str(entry.get("status", ""))
        symbol = str(entry.get("symbol", ""))
        side = str(entry.get("side", ""))
        fills = int(entry.get("filled_quantity", 0) or 0)
        if status in ("FILLED", "PARTIALLY_FILLED"):
            filled[internal] = fills
            delta = fills if side == "BUY" else -fills
            positions[symbol] = positions.get(symbol, 0) + delta
        elif status in ("PENDING",):
            open_orders[internal] = symbol
    return {
        "expected_positions": {s: q for s, q in positions.items() if q},
        "expected_open_orders": open_orders,
        "expected_filled": filled,
    }


def _handle_reconcile(
    args: argparse.Namespace,
    factory: AdapterFactory,
    environ: Mapping[str, str] | None,
    out: Printer,
) -> int:
    adapter = factory(args.broker)
    prior = load_prior_status(environ)
    entries = prior.get("recent_sandbox_orders") or []

    # Rebuild local session expectations into in-memory repositories so the
    # reconciler applies the standard engine.
    orders_repo = InMemoryOrderRepository()
    positions_repo = InMemoryPositionRepository()
    recorded: list[dict[str, Any]] = [
        dict(entry) for entry in entries if entry.get("internal_order_id")
    ]

    expected = _recorded_expectations(recorded)

    actual_orders: list[OrderResult] = []
    for entry in recorded:
        internal = str(entry["internal_order_id"])
        ref = str(entry.get("broker_order_id") or internal)
        record = adapter.get_order_status(ref)
        if record is None:
            continue
        actual_orders.append(
            OrderResult.model_validate(
                {
                    "internal_order_id": internal,
                    "idempotency_key": internal,
                    "broker_order_id": record.order_id,
                    "symbol": record.symbol,
                    "side": record.side,
                    "status": record.status,
                    "requested_quantity": record.quantity,
                    "filled_quantity": record.filled_quantity,
                    "average_fill_price": record.average_price,
                    "timestamp": record.updated_at
                    or record.placed_at
                    or datetime.now(UTC),
                    "reason": record.message,
                }
            )
        )
    actual_positions: list[Position] = adapter.get_positions()

    reconciler = SandboxReconciler(
        adapter,
        order_repository=orders_repo,
        position_repository=positions_repo,
    )
    result = reconciler.end_of_day(
        run_id=f"sandbox-eod-{args.broker}-{datetime.now(UTC).date().isoformat()}",
        expected=expected,
        actual={"actual_positions": actual_positions, "actual_orders": actual_orders},
    )
    decision = reconciler.risk_decision(result)
    _persist_status(
        environ,
        {args.broker: adapter},
        reconciliation_health=summarize_reconciliation(result),
    )
    _print_json(
        out,
        {
            "matched": result.matched,
            "locked": result.locked,
            "lock_reason": result.lock_reason,
            "risk_state": decision.state.value,
            "mismatches": [
                {
                    "kind": m.kind,
                    "symbol": m.symbol,
                    "expected": m.expected,
                    "actual": m.actual,
                    "detail": m.detail,
                }
                for m in result.mismatches
            ],
        },
    )
    return EXIT_OK if result.matched else EXIT_REFUSED


def cli_main(
    argv: Sequence[str] | None = None,
    *,
    factory: AdapterFactory | None = None,
    environ: Mapping[str, str] | None = None,
    out: Printer | None = None,
) -> int:
    """Execute one broker CLI command."""
    printer = out or print
    make_adapter = factory or _default_factory
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "broker_command", None):
        parser.print_help()
        return EXIT_OK
    command = str(args.broker_command)
    try:
        mode = _resolve_cli_mode(environ)
        _require_not_live(mode)
        if command == "health":
            return _handle_health(args, make_adapter, environ, printer)
        if command == "login":
            return _handle_login(args, make_adapter, environ, printer)
        if command in ("funds", "holdings", "positions", "orders"):
            return _handle_read(command, args, make_adapter, environ, printer)
        if command == "sandbox-order":
            return _handle_sandbox_order(args, make_adapter, environ, printer)
        if command == "sandbox-cancel":
            return _handle_sandbox_cancel(args, make_adapter, environ, printer)
        if command == "reconcile":
            return _handle_reconcile(args, make_adapter, environ, printer)
        printer(f"unknown command {command!r}")
        return EXIT_ERROR
    except (LiveTradingDisabledError, SandboxOnlyError) as exc:
        printer(f"refused: {exc}")
        return EXIT_REFUSED
    except BrokerError as exc:
        printer(f"error: {exc}")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(cli_main())
