"""Application service for the local, virtual paper-trading account.

The service has three strict boundaries:

* Upstox is a **read-only quote source**.
* Every order is a virtual record in :class:`PaperLedger`; no broker order API
  is imported or reachable from this package.
* A strategy must be explicitly marked ``paper_approved`` after backtesting
  before it can rebalance the virtual portfolio.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from config.costs import SCENARIO_MARKET_CONDITIONS, CostScenario, load_charge_table

from datahub import state as sysstate
from datahub.quotes import QuoteChain, build_quote_chain

from .ledger import PaperLedger
from .market_data import (
    MarketDataUnavailable,
    MarketQuote,
    UpstoxMarketData,
    load_nifty_instruments,
)

DEFAULT_WATCHLIST = ("NIFTY_50", "RELIANCE", "HDFCBANK", "ICICIBANK", "TCS")
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_RISK_POLICY = {
    "max_position_weight": 0.15,
    "max_gross_exposure": 1.00,
    "daily_loss_limit": 0.03,
    "max_drawdown": 0.15,
    "max_orders_per_rebalance": 30,
}
DEFAULT_REGISTRY = {
    "momrem": {
        "label": "MomReM momentum + regime filter",
        "status": "RESEARCH_ONLY",
        "paper_approved": False,
        "mode": "DAILY",
        "min_rebalance_seconds": 86_400,
        "reason": "Requires a fresh independent review before paper allocation.",
    }
}


class PaperTradingService:
    """Coordinates data snapshots, virtual fills, P&L and paper-gate status."""

    def __init__(
        self,
        *,
        root: Path | str = ".",
        ledger: PaperLedger | None = None,
        market_data: Any | None = None,
        registry_path: Path | str | None = None,
        quote_stale_seconds: int = 90,
    ) -> None:
        self.root = Path(root)
        self.ledger = ledger or PaperLedger(self.root / "var" / "paper_trading.sqlite")
        self.registry_path = Path(
            registry_path or self.root / "config" / "paper_strategies.json"
        )
        self.quote_stale_seconds = int(quote_stale_seconds)
        settings = self.ledger.settings()
        sandbox = settings["data_mode"] == "SANDBOX"
        self.market_data = market_data or UpstoxMarketData.from_environment(
            sandbox=sandbox
        )
        # Load the entire Nifty 500 PIT mapping. The historical membership has
        # more than 500 unique symbols, while one Upstox request is chunked at
        # the adapter's 500-instrument limit.
        self._instruments = load_nifty_instruments(self.root, index_name="nifty500")
        # Quote chain: UPSTOX -> SIM -> EOD.  Before this, a missing access token
        # made every refresh fail hard, so the tape showed ERROR/UNAVAILABLE on
        # every load while the Live Terminal next door rendered a full simulated
        # tape from the same repository data.  Degradation is now explicit and
        # every quote carries its source.
        self.quote_chain: QuoteChain = build_quote_chain(
            self.market_data, self._instruments
        )
        # Which client the chain was built against.  Reassigning
        # ``market_data`` must rebuild it, otherwise the tape keeps pricing
        # through the stale client (tests do this; re-authenticating does too).
        self._chain_market_data: Any = self.market_data

    def _quote_chain(self) -> QuoteChain:
        """Return the quote chain, rebuilding it if ``market_data`` changed."""
        if self._chain_market_data is not self.market_data:
            self.quote_chain = build_quote_chain(
                self.market_data, self._instruments
            )
            self._chain_market_data = self.market_data
        return self.quote_chain

    # -- strategy gate -----------------------------------------------------

    def strategies(self) -> dict[str, dict[str, Any]]:
        """Return the audited paper-strategy registry, never executable code."""
        result = {key: dict(value) for key, value in DEFAULT_REGISTRY.items()}
        if not self.registry_path.is_file():
            return result
        try:
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return result
        entries = (
            payload.get("strategies", payload) if isinstance(payload, Mapping) else {}
        )
        if not isinstance(entries, Mapping):
            return result
        for key, value in entries.items():
            if not isinstance(value, Mapping):
                continue
            name = str(key).strip().lower()
            if not name:
                continue
            result[name] = {
                "label": str(value.get("label", name)),
                "status": str(value.get("status", "RESEARCH_ONLY")),
                "paper_approved": bool(value.get("paper_approved", False)),
                "mode": str(value.get("mode", "DAILY")).upper(),
                "min_rebalance_seconds": max(
                    60, int(value.get("min_rebalance_seconds", 86_400))
                ),
                "reason": str(value.get("reason", "")),
            }
        return result

    def is_paper_approved(self, strategy_id: str) -> bool:
        return bool(
            self.strategies().get(strategy_id.lower(), {}).get("paper_approved")
        )

    # -- lifecycle ---------------------------------------------------------

    def configure(self, capital: float, data_mode: str) -> dict[str, Any]:
        settings = self.ledger.configure(capital, data_mode)
        self.market_data = UpstoxMarketData.from_environment(
            sandbox=settings["data_mode"] == "SANDBOX"
        )
        self.quote_chain = build_quote_chain(self.market_data, self._instruments)
        self._chain_market_data = self.market_data
        return self.status()

    def set_watchlist(self, symbols: list[str]) -> dict[str, Any]:
        cleaned = list(
            dict.fromkeys(
                str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
            )
        )
        if "NIFTY_50" not in cleaned:
            cleaned.insert(0, "NIFTY_50")
        self._instrument_map(cleaned)
        self.ledger.set_watchlist(cleaned)
        return self.status()

    @staticmethod
    def _validate_risk_policy(source: Mapping[str, Any]) -> dict[str, float | int]:
        policy: dict[str, float | int] = dict(DEFAULT_RISK_POLICY)
        for name in DEFAULT_RISK_POLICY:
            if name not in source:
                continue
            if name == "max_orders_per_rebalance":
                number = float(source[name])
                if not number.is_integer():
                    raise ValueError("max_orders_per_rebalance must be a whole number")
                policy[name] = int(number)
            else:
                policy[name] = float(source[name])
        if not 0 < float(policy["max_position_weight"]) <= 1:
            raise ValueError("max_position_weight must be in (0, 1]")
        if not 0 < float(policy["max_gross_exposure"]) <= 1:
            raise ValueError("max_gross_exposure must be in (0, 1]")
        if not 0 < float(policy["daily_loss_limit"]) <= 1:
            raise ValueError("daily_loss_limit must be in (0, 1]")
        if not 0 < float(policy["max_drawdown"]) <= 1:
            raise ValueError("max_drawdown must be in (0, 1]")
        if not 1 <= int(policy["max_orders_per_rebalance"]) <= 100:
            raise ValueError("max_orders_per_rebalance must be in [1, 100]")
        return policy

    def risk_policy(self) -> dict[str, float | int]:
        settings = self.ledger.settings()
        return self._validate_risk_policy(settings.get("risk_policy") or {})

    def set_risk_policy(self, values: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(values) - set(DEFAULT_RISK_POLICY)
        if unknown:
            raise ValueError(
                "unknown risk-policy fields: " + ", ".join(sorted(unknown))
            )
        candidate = self.risk_policy()
        candidate.update(values)
        self.ledger.set_risk_policy(self._validate_risk_policy(candidate))
        return self.status()

    def set_auto_paper(
        self,
        *,
        enabled: bool,
        strategy_id: str,
        confirmation: str,
    ) -> dict[str, Any]:
        if enabled:
            if confirmation != "ENABLE AUTO PAPER":
                raise ValueError(
                    "automation confirmation must be exactly ENABLE AUTO PAPER"
                )
            definition = self.strategies().get(strategy_id.lower())
            if not definition or not definition["paper_approved"]:
                raise ValueError("only a paper-approved strategy can be automated")
            self.ledger.set_auto_paper(
                enabled=True,
                strategy_id=strategy_id.lower(),
                interval_seconds=int(definition["min_rebalance_seconds"]),
            )
        else:
            self.ledger.set_auto_paper(
                enabled=False, strategy_id=None, interval_seconds=86_400
            )
        return self.status()

    def start_monitor(self) -> dict[str, Any]:
        self.ledger.start(None)
        return self.status()

    def pause(self) -> dict[str, Any]:
        self.ledger.pause()
        return self.status()

    def reset(self, confirmation: str, capital: float | None = None) -> dict[str, Any]:
        if confirmation != "RESET PAPER":
            raise ValueError("reset confirmation must be exactly RESET PAPER")
        self.ledger.reset(capital)
        return self.status()

    # -- market data and P&L ----------------------------------------------

    def _instrument_map(self, symbols: list[str]) -> dict[str, str]:
        unknown = [symbol for symbol in symbols if symbol not in self._instruments]
        if unknown:
            raise MarketDataUnavailable(
                "no verified Upstox instrument mapping for: "
                + ", ".join(sorted(unknown))
            )
        return {symbol: self._instruments[symbol] for symbol in symbols}

    def refresh_quotes(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Fetch and persist a data-only quote snapshot, then mark virtual P&L."""
        settings = self.ledger.settings()
        held = [str(item["symbol"]) for item in self.ledger.positions()]
        watchlist = list(settings.get("watchlist") or DEFAULT_WATCHLIST)
        names = list(
            dict.fromkeys((symbols if symbols is not None else watchlist) + held)
        )
        try:
            self._instrument_map(names)  # surface unmapped symbols as a warning
        except MarketDataUnavailable as exc:
            self.ledger.record_event("quote_instrument_map_incomplete", {"error": str(exc)})
        chain = self._quote_chain()
        quotes = chain.fetch(names)
        summary = chain.summarise(quotes)
        missing = sorted(set(names) - set(quotes))
        # An error is only recorded when NOTHING could be priced.  Running on the
        # labelled simulator is a deliberate, healthy degraded state, not a fault.
        error = None
        if not quotes:
            error = (
                self.quote_chain.last_error
                or f"no quote source could price: {', '.join(missing)}"
            )
        elif missing:
            self.ledger.record_event(
                "quote_refresh_partial",
                {"missing": missing, "source": summary["source"]},
            )
        self.ledger.record_quote_health(error)
        self.ledger.record_marks(
            [quote.as_market_quote_dict() for quote in quotes.values()]
        )
        sysstate.beat(
            "quote_refreshed",
            {
                "source": summary["source"],
                "quoted": summary["quoted"],
                "requested": len(names),
                "missing": missing,
                "error": error,
            },
        )
        benchmark = quotes.get(str(settings.get("benchmark_symbol", "NIFTY_50")))
        if benchmark:
            self.ledger.set_benchmark_start_price(benchmark.last_price)
        result = self._mark_to_market(quotes, error=error)
        result["quote_source"] = summary["source"]
        result["quote_source_counts"] = summary["counts"]
        result["quote_source_note"] = summary["note"]
        return result

    def _mark_to_market(
        self,
        quotes: Mapping[str, MarketQuote],
        *,
        error: str | None,
    ) -> dict[str, Any]:
        positions = self.ledger.positions()
        stored = self.ledger.latest_marks([str(p["symbol"]) for p in positions])
        cash = float(self.ledger.settings()["cash"])
        market_value = 0.0
        unrealized = 0.0
        realized = self.ledger.realized_pnl_total()
        missing = []
        for position in positions:
            symbol = str(position["symbol"])
            quote = quotes.get(symbol)
            last = (
                quote.last_price
                if quote
                else (float(stored[symbol]["last_price"]) if symbol in stored else None)
            )
            if last is None:
                missing.append(symbol)
                # No fabricated mark: cost is retained only as a conservative display fallback.
                last = float(position["average_entry_cost"] or 0.0)
            quantity = int(position["quantity"])
            market_value += quantity * last
            unrealized += quantity * (
                last - float(position["average_entry_cost"] or 0.0)
            )
        sources = {q.source for q in quotes.values()}
        if error and not quotes:
            quote_status = "UNAVAILABLE"
        elif "UPSTOX" in sources and not missing:
            quote_status = "LIVE"
        elif "UPSTOX" in sources:
            quote_status = "PARTIAL"
        elif "SIM" in sources:
            quote_status = "SIM"
        elif "EOD" in sources:
            quote_status = "EOD"
        else:
            quote_status = "UNAVAILABLE" if missing else "NO_POSITIONS"
        snapshot = {
            "equity": cash + market_value,
            "cash": cash,
            "market_value": market_value,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "quote_status": quote_status,
            "quote_error": error,
            "unmarked_symbols": missing,
        }
        self.ledger.record_equity(snapshot)
        return snapshot

    # -- target / virtual execution ---------------------------------------

    def _momrem_target(self) -> tuple[dict[str, int], dict[str, Any]]:
        """Build a daily target from the established MomReM signal module."""
        from dashboard.strategy_dashboard import compute_momrem_signal

        capital = float(self.ledger.settings()["initial_capital"])
        signal = compute_momrem_signal(capital)
        if not signal.get("fresh"):
            raise ValueError(
                f"daily signal is stale as of {signal.get('as_of')}; refresh validated EOD data first"
            )
        if signal["position"]["state"] == "IN_CASH":
            return {}, signal
        return {
            str(item["symbol"]).upper(): int(item["qty"])
            for item in signal["basket"]
            if int(item["qty"]) > 0
        }, signal

    def preview_rebalance(self, strategy_id: str) -> dict[str, Any]:
        strategy_id = strategy_id.strip().lower()
        if not self.is_paper_approved(strategy_id):
            raise ValueError("strategy is not paper-approved by the research gate")
        if strategy_id != "momrem":
            raise ValueError(
                "no paper target builder has been registered for this strategy"
            )
        target, signal = self._momrem_target()
        current = {
            str(p["symbol"]): int(p["quantity"]) for p in self.ledger.positions()
        }
        symbols = sorted(set(target) | set(current))
        # New targets get a real quote snapshot too, so virtual fills never use old EOD close.
        try:
            quotes = self.market_data.fetch_quotes(self._instrument_map(symbols))
            self.ledger.record_marks([quote.to_dict() for quote in quotes.values()])
            stale_symbols = [
                symbol
                for symbol, quote in quotes.items()
                if (datetime.now(UTC) - quote.timestamp.astimezone(UTC)).total_seconds()
                > self.quote_stale_seconds * 3
            ]
            if stale_symbols:
                return {
                    "strategy_id": strategy_id,
                    "ready": False,
                    "reason": "stale source quote(s): "
                    + ", ".join(sorted(stale_symbols)),
                    "signal": signal,
                    "orders": [],
                }
        except MarketDataUnavailable as exc:
            return {
                "strategy_id": strategy_id,
                "ready": False,
                "reason": str(exc),
                "signal": signal,
                "orders": [],
            }
        orders = []
        for symbol in symbols:
            desired, held = target.get(symbol, 0), current.get(symbol, 0)
            delta = desired - held
            if not delta:
                continue
            quote = quotes.get(symbol)
            if quote is None:
                orders.append(
                    {"symbol": symbol, "status": "NO_QUOTE", "target_quantity": desired}
                )
                continue
            side = "BUY" if delta > 0 else "SELL"
            price = quote.ask_price if side == "BUY" else quote.bid_price
            if price is None:
                orders.append(
                    {
                        "symbol": symbol,
                        "status": "NO_BID_ASK",
                        "target_quantity": desired,
                    }
                )
                continue
            amount = abs(delta) * price
            costs = self._costs(side, amount)
            orders.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": abs(delta),
                    "target_quantity": desired,
                    "reference_price": quote.last_price,
                    "estimated_fill_price": price,
                    "estimated_notional": amount,
                    "estimated_charges": costs,
                    "quote_timestamp": quote.timestamp.isoformat(),
                    "status": "READY",
                }
            )
        risk = self._pretrade_risk(target, quotes, len(orders))
        ready = (
            bool(orders)
            and all(item["status"] == "READY" for item in orders)
            and bool(risk["allowed"])
        )
        return {
            "strategy_id": strategy_id,
            "ready": ready,
            "reason": (
                None
                if ready
                else (
                    "risk guard blocked paper rebalance: " + ", ".join(risk["breaches"])
                    if risk["breaches"]
                    else "at least one virtual order is not ready"
                )
            ),
            "signal": signal,
            "orders": orders,
            "risk": risk,
            "cost_model": self._cost_model_metadata(),
        }

    @staticmethod
    def _cost_model_metadata() -> dict[str, Any]:
        table = load_charge_table()
        conditions = SCENARIO_MARKET_CONDITIONS[CostScenario.BASE]
        return {
            "table_version": table.table_version,
            "scenario": CostScenario.BASE,
            "fill_price": "observed best bid/ask",
            "additional_slippage_bps": conditions["slippage_bps"],
            "buy_regulatory_bps": table.buy_bps,
            "sell_regulatory_bps": table.sell_bps,
        }

    @staticmethod
    def _costs(side: str, value: float) -> float:
        """Delivery-equity charge estimate using the shared configurable table.

        Fills use the observed bid/ask, so its actual spread replaces the
        generic spread assumption. A separate base-scenario slippage allowance
        remains in the paper cost estimate.
        """
        table = load_charge_table()
        conditions = SCENARIO_MARKET_CONDITIONS[CostScenario.BASE]
        side_bps = table.buy_bps if side == "BUY" else table.sell_bps
        return round(
            value * (side_bps + float(conditions["slippage_bps"])) / 10_000.0, 2
        )

    @staticmethod
    def _local_date(timestamp: str) -> date | None:
        try:
            return (
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                .astimezone(IST)
                .date()
            )
        except (AttributeError, TypeError, ValueError):
            return None

    def _risk_summary(
        self,
        *,
        equity: float,
        market_value: float,
        history: list[dict[str, Any]],
        position_values: Mapping[str, float] | None = None,
    ) -> dict[str, Any]:
        """Evaluate portfolio guardrails without ever placing a broker order."""
        policy = self.risk_policy()
        initial = float(self.ledger.settings()["initial_capital"])
        high_water = max(
            [initial, *(float(x["equity"]) for x in history)], default=initial
        )
        today = datetime.now(IST).date()
        today_points = [
            point
            for point in history
            if self._local_date(str(point.get("recorded_at", ""))) == today
        ]
        day_start = float(today_points[0]["equity"]) if today_points else equity
        daily_pnl = equity - day_start
        drawdown = max(0.0, high_water - equity)
        gross_exposure = market_value / equity if equity > 0 else 0.0
        weights = {
            symbol: value / equity if equity > 0 else 0.0
            for symbol, value in (position_values or {}).items()
        }
        breaches = []
        if daily_pnl <= -(initial * float(policy["daily_loss_limit"])):
            breaches.append("daily_loss_limit")
        if drawdown >= high_water * float(policy["max_drawdown"]):
            breaches.append("max_drawdown")
        if gross_exposure > float(policy["max_gross_exposure"]):
            breaches.append("max_gross_exposure")
        oversized = sorted(
            symbol
            for symbol, weight in weights.items()
            if weight > float(policy["max_position_weight"])
        )
        if oversized:
            breaches.append("max_position_weight: " + ", ".join(oversized))
        return {
            "policy": policy,
            "daily_pnl": round(daily_pnl, 2),
            "high_water_equity": round(high_water, 2),
            "drawdown": round(drawdown, 2),
            "drawdown_pct": round(drawdown / high_water, 6) if high_water else 0.0,
            "gross_exposure": round(gross_exposure, 6),
            "position_weights": {
                key: round(value, 6) for key, value in weights.items()
            },
            "breaches": breaches,
            "allowed": not breaches,
        }

    def _pretrade_risk(
        self,
        target: Mapping[str, int],
        quotes: Mapping[str, MarketQuote],
        order_count: int,
    ) -> dict[str, Any]:
        current = self.ledger.latest_equity()
        settings = self.ledger.settings()
        equity = float(current["equity"] if current else settings["initial_capital"])
        values = {
            symbol: float(quantity) * float(quotes[symbol].last_price)
            for symbol, quantity in target.items()
            if symbol in quotes
        }
        summary = self._risk_summary(
            equity=equity,
            market_value=sum(values.values()),
            history=self.ledger.equity_history(limit=5000),
            position_values=values,
        )
        policy = summary["policy"]
        if order_count > int(policy["max_orders_per_rebalance"]):
            summary["breaches"].append("max_orders_per_rebalance")
        planned_cash = float(settings["cash"])
        for symbol, quantity in target.items():
            held = next(
                (
                    int(row["quantity"])
                    for row in self.ledger.positions()
                    if str(row["symbol"]) == symbol
                ),
                0,
            )
            delta = quantity - held
            if not delta or symbol not in quotes:
                continue
            quote = quotes[symbol]
            price = quote.ask_price if delta > 0 else quote.bid_price
            if price is None:
                continue
            notional = abs(delta) * float(price)
            charges = self._costs("BUY" if delta > 0 else "SELL", notional)
            planned_cash += -(notional + charges) if delta > 0 else notional - charges
        if planned_cash < -0.005:
            summary["breaches"].append("insufficient_virtual_cash")
        summary["projected_cash"] = round(planned_cash, 2)
        summary["allowed"] = not summary["breaches"]
        return summary

    def _apply_preview(
        self, strategy_id: str, preview: Mapping[str, Any], *, source: str
    ) -> dict[str, Any]:
        """Write already validated fills to the local SQLite ledger only."""
        # Sell first to free virtual cash; then buy at the observed ask.  This has no
        # connection to any real Upstox order endpoint.
        prepared = sorted(preview["orders"], key=lambda order: order["side"] != "SELL")
        results = []
        for order in prepared:
            result = self.ledger.execute_virtual_fill(
                strategy_id=strategy_id,
                symbol=order["symbol"],
                side=order["side"],
                quantity=int(order["quantity"]),
                fill_price=float(order["estimated_fill_price"]),
                charges=float(order["estimated_charges"]),
                source=source,
                quote_timestamp=str(order["quote_timestamp"]),
            )
            results.append(result)
        mark = self.refresh_quotes([])
        return {"preview": preview, "results": results, "mark": mark}

    def execute_rebalance(self, strategy_id: str, confirmation: str) -> dict[str, Any]:
        if sysstate.is_killed():
            switch = sysstate.kill_switch()
            raise ValueError(
                "kill switch is armed"
                + (f" ({switch.get('reason')})" if switch.get("reason") else "")
                + " — disarm it on the Operations page before rebalancing"
            )
        if confirmation != "PAPER REBALANCE":
            raise ValueError(
                "paper rebalance confirmation must be exactly PAPER REBALANCE"
            )
        if not self.ledger.settings()["running"]:
            raise ValueError("start the paper monitor before rebalancing")
        preview = self.preview_rebalance(strategy_id)
        if not preview["ready"]:
            raise ValueError("rebalance is not ready; no virtual orders were created")
        return self._apply_preview(
            strategy_id, preview, source="upstox_quote_read_only"
        )

    def run_automation_once(self) -> dict[str, Any]:
        """Optionally rebalance a specifically approved virtual strategy.

        This method is deliberately called by the local quote poller only.  It
        reads data, applies the normal strategy/risk checks, and writes virtual
        fills to SQLite.  It has no path to a broker order API.
        """
        if sysstate.is_killed():
            return {"ran": False, "reason": "kill switch is armed"}
        settings = self.ledger.settings()
        if not settings["running"] or not settings["auto_paper_enabled"]:
            return {"ran": False, "reason": "automatic paper mode is disabled"}
        strategy_id = str(settings.get("auto_strategy") or "").lower()
        definition = self.strategies().get(strategy_id)
        if not definition or not definition["paper_approved"]:
            self.ledger.record_event(
                "auto_paper_blocked", {"reason": "strategy is no longer paper-approved"}
            )
            return {"ran": False, "reason": "strategy is not paper-approved"}
        now = datetime.now(IST)
        market_minute = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not 555 <= market_minute <= 930:
            return {"ran": False, "reason": "outside NSE cash market hours"}
        last = settings.get("last_auto_rebalance_at")
        if last:
            try:
                elapsed = (
                    datetime.now(UTC) - datetime.fromisoformat(last)
                ).total_seconds()
                if elapsed < int(settings["auto_interval_seconds"]):
                    return {"ran": False, "reason": "interval has not elapsed"}
            except (TypeError, ValueError):
                pass
        self.ledger.mark_auto_rebalance()
        try:
            preview = self.preview_rebalance(strategy_id)
            if not preview["ready"]:
                self.ledger.record_event(
                    "auto_paper_skipped",
                    {"strategy_id": strategy_id, "reason": preview.get("reason")},
                )
                return {"ran": False, "reason": preview.get("reason", "not ready")}
            result = self._apply_preview(
                strategy_id, preview, source="auto_paper_read_only"
            )
            self.ledger.record_event(
                "auto_paper_rebalanced",
                {"strategy_id": strategy_id, "fills": len(result["results"])},
            )
            sysstate.beat(
                "paper_rebalance",
                {"strategy_id": strategy_id, "fills": len(result["results"])},
            )
            return {"ran": True, "fills": len(result["results"])}
        except Exception as exc:  # nosec B110 - keep local dashboard available
            self.ledger.record_event(
                "auto_paper_failed", {"strategy_id": strategy_id, "error": str(exc)}
            )
            return {"ran": False, "reason": "automatic paper rebalance failed"}

    # -- read model --------------------------------------------------------

    def _quote_health(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        last_success = settings.get("last_quote_success_at")
        age_seconds: float | None = None
        if last_success:
            try:
                age_seconds = max(
                    0.0,
                    (
                        datetime.now(UTC) - datetime.fromisoformat(str(last_success))
                    ).total_seconds(),
                )
            except ValueError:
                last_success = None
        watchlist = list(settings.get("watchlist") or DEFAULT_WATCHLIST)
        marks = self.ledger.latest_marks(watchlist)
        source_ages: dict[str, float] = {}
        for symbol, mark in marks.items():
            try:
                source_ages[symbol] = max(
                    0.0,
                    (
                        datetime.now(UTC)
                        - datetime.fromisoformat(str(mark["source_timestamp"]))
                    ).total_seconds(),
                )
            except (KeyError, TypeError, ValueError):
                source_ages[symbol] = float("inf")
        stale_symbols = sorted(
            symbol
            for symbol, source_age in source_ages.items()
            if source_age > self.quote_stale_seconds * 3
        )
        status = "NEVER_REFRESHED"
        if settings.get("last_quote_error"):
            status = "ERROR"
        elif age_seconds is not None:
            status = (
                "STALE" if age_seconds > self.quote_stale_seconds * 3 else "HEALTHY"
            )
        if status == "HEALTHY" and stale_symbols:
            status = "STALE"
        # A healthy-but-simulated feed is not an error: report the source so the
        # UI can say "SIM — not real prices" instead of a red ERROR badge.
        source = self.quote_chain.primary_source
        if status == "HEALTHY" and source != "UPSTOX":
            status = "HEALTHY_SIM" if source == "SIM" else "HEALTHY_EOD"
        return {
            "status": status,
            "source": source,
            "last_success_at": last_success,
            "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
            "stale_symbols": stale_symbols,
            "error": settings.get("last_quote_error"),
            "chain": self.quote_chain.status(),
        }

    @staticmethod
    def _performance(
        history: list[dict[str, Any]], initial_capital: float, equity: float
    ) -> dict[str, Any]:
        equities = [initial_capital, *(float(point["equity"]) for point in history)]
        high_water = equities[0]
        max_drawdown = 0.0
        for point in equities:
            high_water = max(high_water, point)
            max_drawdown = max(max_drawdown, high_water - point)
        pnl = equity - initial_capital
        return {
            "net_pnl": round(pnl, 2),
            "return_pct": round(pnl / initial_capital * 100, 4)
            if initial_capital
            else None,
            "high_water_equity": round(high_water, 2),
            "max_drawdown": round(max_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown / high_water * 100, 4)
            if high_water
            else None,
            "snapshots": len(history),
        }

    def _strategy_performance(
        self, equity: float, initial_capital: float
    ) -> list[dict[str, Any]]:
        fills = [
            item
            for item in self.ledger.all_orders()
            if str(item["status"]).upper() == "FILLED"
        ]
        by_strategy: dict[str, int] = {}
        for fill in fills:
            name = str(fill["strategy_id"])
            by_strategy[name] = by_strategy.get(name, 0) + 1
        one_strategy = next(iter(by_strategy), None) if len(by_strategy) == 1 else None
        rows = []
        for strategy_id, definition in self.strategies().items():
            count = by_strategy.get(strategy_id, 0)
            item: dict[str, Any] = {
                "strategy_id": strategy_id,
                "label": definition["label"],
                "paper_approved": definition["paper_approved"],
                "filled_orders": count,
                "profitability": "NO_TRADES" if not count else "UNATTRIBUTED",
                "net_pnl": None,
            }
            # This ledger intentionally allows only a single virtual strategy
            # to receive whole-account attribution. Mixed strategy accounts
            # are explicitly labelled rather than manufacturing attribution.
            if count and one_strategy == strategy_id:
                net_pnl = equity - initial_capital
                item["net_pnl"] = round(net_pnl, 2)
                item["profitability"] = "PROFITABLE" if net_pnl >= 0 else "LOSS"
            rows.append(item)
        return rows

    def status(self) -> dict[str, Any]:
        settings = self.ledger.settings()
        positions = self.ledger.positions()
        marks = self.ledger.latest_marks([str(p["symbol"]) for p in positions])
        history = self.ledger.equity_history(limit=240)
        current_mark = self.ledger.latest_equity()
        lifetime_realized = self.ledger.realized_pnl_total()
        if current_mark is None:
            current_mark = {
                "equity": float(settings["cash"]),
                "cash": float(settings["cash"]),
                "market_value": 0.0,
                "realized_pnl": lifetime_realized,
                "unrealized_pnl": 0.0,
                "quote_status": "NO_POSITIONS" if not positions else "UNMARKED",
                "quote_error": None,
                "unmarked_symbols": [str(item["symbol"]) for item in positions],
            }
        else:
            current_mark = dict(current_mark)
            current_mark["realized_pnl"] = lifetime_realized
        enriched = []
        position_values: dict[str, float] = {}
        for position in positions:
            item = dict(position)
            mark = marks.get(str(item["symbol"]))
            last = float(mark["last_price"]) if mark else None
            item["last_price"] = last
            item["market_value"] = (
                round(last * int(item["quantity"]), 2) if last is not None else None
            )
            if item["market_value"] is not None:
                position_values[str(item["symbol"])] = float(item["market_value"])
            item["unrealized_pnl"] = (
                round(
                    (last - float(item["average_entry_cost"])) * int(item["quantity"]),
                    2,
                )
                if last is not None
                else None
            )
            item["quote_timestamp"] = mark.get("source_timestamp") if mark else None
            enriched.append(item)
        watchlist = list(settings.get("watchlist") or DEFAULT_WATCHLIST)
        watch_marks = self.ledger.latest_marks(watchlist)
        equity = float(current_mark["equity"])
        initial = float(settings["initial_capital"])
        performance = self._performance(history, initial, equity)
        risk = self._risk_summary(
            equity=equity,
            market_value=float(current_mark["market_value"]),
            history=history,
            position_values=position_values,
        )
        benchmark = watch_marks.get(str(settings.get("benchmark_symbol", "NIFTY_50")))
        benchmark_start = settings.get("benchmark_start_price")
        benchmark_return = None
        if benchmark and benchmark_start:
            benchmark_return = round(
                (float(benchmark["last_price"]) / float(benchmark_start) - 1) * 100,
                4,
            )
        return {
            "paper_only": True,
            "kill_switch": sysstate.kill_switch(),
            "quote_chain": self.quote_chain.status(),
            "settings": settings,
            "market_data": self.market_data.connection_status(),
            "quote_refresh_seconds": self.quote_stale_seconds,
            "quote_health": self._quote_health(settings),
            "portfolio": current_mark,
            "performance": performance,
            "risk": risk,
            "benchmark": {
                "symbol": settings.get("benchmark_symbol", "NIFTY_50"),
                "start_price": benchmark_start,
                "last_price": benchmark.get("last_price") if benchmark else None,
                "return_pct": benchmark_return,
            },
            "strategy_performance": self._strategy_performance(equity, initial),
            "positions": enriched,
            "watchlist_quotes": [
                watch_marks[symbol] for symbol in watchlist if symbol in watch_marks
            ],
            "orders": self.ledger.order_history(),
            "equity_history": history,
            "strategies": self.strategies(),
            "events": self.ledger.events(),
            "server_time": datetime.now(UTC).isoformat(),
        }

    def audit(self) -> dict[str, Any]:
        """Reconcile local cash and positions against immutable virtual fills."""
        orders = [
            order
            for order in self.ledger.all_orders()
            if str(order["status"]).upper() == "FILLED"
        ]
        settings = self.ledger.settings()
        expected_cash = float(settings["initial_capital"])
        expected_quantities: dict[str, int] = {}
        for order in orders:
            side = str(order["side"])
            quantity = int(order["quantity"])
            symbol = str(order["symbol"])
            notional = float(order["notional"])
            charges = float(order["charges"])
            expected_cash += (
                -(notional + charges) if side == "BUY" else notional - charges
            )
            expected_quantities[symbol] = expected_quantities.get(symbol, 0) + (
                quantity if side == "BUY" else -quantity
            )
        actual_quantities = {
            str(position["symbol"]): int(position["quantity"])
            for position in self.ledger.all_positions()
        }
        cash_difference = round(float(settings["cash"]) - expected_cash, 2)
        quantity_mismatches = {
            symbol: {"expected": expected, "actual": actual_quantities.get(symbol, 0)}
            for symbol, expected in expected_quantities.items()
            if actual_quantities.get(symbol, 0) != expected
        }
        passed = abs(cash_difference) < 0.01 and not quantity_mismatches
        return {
            "passed": passed,
            "filled_order_count": len(orders),
            "expected_cash": round(expected_cash, 2),
            "actual_cash": round(float(settings["cash"]), 2),
            "cash_difference": cash_difference,
            "quantity_mismatches": quantity_mismatches,
            "checked_at": datetime.now(UTC).isoformat(),
            "scope": "local SQLite virtual ledger only; no broker account is queried",
        }

    def export_csv(self, dataset: str) -> str:
        """Export a bounded local-ledger dataset; credentials are never exported."""
        name = dataset.strip().lower()
        data: dict[str, list[dict[str, Any]]] = {
            "orders": self.ledger.all_orders(),
            "positions": self.ledger.all_positions(),
            "equity": self.ledger.equity_history(limit=5000),
            "marks": self.ledger.marks_history(limit=10_000),
            "events": self.ledger.events(limit=500),
        }
        if name not in data:
            raise ValueError(
                "export dataset must be orders, positions, equity, marks, or events"
            )
        rows = data[name]
        if name == "events":
            rows = [
                {**row, "detail": json.dumps(row.get("detail", {}), sort_keys=True)}
                for row in rows
            ]
        fields_by_dataset = {
            "orders": [
                "id",
                "created_at",
                "strategy_id",
                "symbol",
                "side",
                "quantity",
                "reference_price",
                "fill_price",
                "notional",
                "charges",
                "status",
                "reason",
                "source",
                "quote_timestamp",
            ],
            "positions": [
                "symbol",
                "quantity",
                "average_entry_cost",
                "realized_pnl",
                "opened_at",
                "updated_at",
            ],
            "equity": [
                "id",
                "recorded_at",
                "equity",
                "cash",
                "market_value",
                "realized_pnl",
                "unrealized_pnl",
                "quote_status",
            ],
            "marks": [
                "id",
                "recorded_at",
                "symbol",
                "instrument_key",
                "last_price",
                "bid_price",
                "ask_price",
                "volume",
                "source_timestamp",
                "source",
            ],
            "events": ["id", "created_at", "event_type", "detail"],
        }
        fields = fields_by_dataset[name]
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue()
