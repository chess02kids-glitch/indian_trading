"""Application service for the local, virtual paper-trading account.

The service has three strict boundaries:

* Upstox is a **read-only quote source**.
* Every order is a virtual record in :class:`PaperLedger`; no broker order API
  is imported or reachable from this package.
* A strategy must be explicitly marked ``paper_approved`` after backtesting
  before it can rebalance the virtual portfolio.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from config.costs import SCENARIO_MARKET_CONDITIONS, CostScenario, load_charge_table

from .ledger import PaperLedger
from .market_data import (
    MarketDataUnavailable,
    MarketQuote,
    UpstoxMarketData,
    load_nifty_instruments,
)

DEFAULT_WATCHLIST = ("NIFTY_50", "RELIANCE", "HDFCBANK", "ICICIBANK", "TCS")
DEFAULT_REGISTRY = {
    "momrem": {
        "label": "MomReM momentum + regime filter",
        "status": "RESEARCH_ONLY",
        "paper_approved": False,
        "mode": "DAILY",
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
        held = [str(item["symbol"]) for item in self.ledger.positions()]
        names = list(dict.fromkeys((symbols or list(DEFAULT_WATCHLIST)) + held))
        try:
            quotes = self.market_data.fetch_quotes(self._instrument_map(names))
        except MarketDataUnavailable as exc:
            return self._mark_to_market({}, error=str(exc))
        self.ledger.record_marks([quote.to_dict() for quote in quotes.values()])
        return self._mark_to_market(quotes, error=None)

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
        realized = sum(float(item["realized_pnl"]) for item in positions)
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
        quote_status = (
            "LIVE"
            if not error and not missing
            else ("PARTIAL" if not error else "UNAVAILABLE")
        )
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
        return {
            "strategy_id": strategy_id,
            "ready": bool(orders) and all(item["status"] == "READY" for item in orders),
            "signal": signal,
            "orders": orders,
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

    def execute_rebalance(self, strategy_id: str, confirmation: str) -> dict[str, Any]:
        if confirmation != "PAPER REBALANCE":
            raise ValueError(
                "paper rebalance confirmation must be exactly PAPER REBALANCE"
            )
        if not self.ledger.settings()["running"]:
            raise ValueError("start the paper monitor before rebalancing")
        preview = self.preview_rebalance(strategy_id)
        if not preview["ready"]:
            raise ValueError("rebalance is not ready; no virtual orders were created")
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
                source="upstox_quote_read_only",
                quote_timestamp=str(order["quote_timestamp"]),
            )
            results.append(result)
        mark = self.refresh_quotes([])
        return {"preview": preview, "results": results, "mark": mark}

    # -- read model --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        settings = self.ledger.settings()
        positions = self.ledger.positions()
        marks = self.ledger.latest_marks([str(p["symbol"]) for p in positions])
        current_mark = self.ledger.latest_equity()
        if current_mark is None:
            current_mark = {
                "equity": float(settings["cash"]),
                "cash": float(settings["cash"]),
                "market_value": 0.0,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "quote_status": "NO_POSITIONS" if not positions else "UNMARKED",
                "quote_error": None,
                "unmarked_symbols": [str(item["symbol"]) for item in positions],
            }
        enriched = []
        for position in positions:
            item = dict(position)
            mark = marks.get(str(item["symbol"]))
            last = float(mark["last_price"]) if mark else None
            item["last_price"] = last
            item["market_value"] = (
                round(last * int(item["quantity"]), 2) if last else None
            )
            item["unrealized_pnl"] = (
                round(
                    (last - float(item["average_entry_cost"])) * int(item["quantity"]),
                    2,
                )
                if last
                else None
            )
            item["quote_timestamp"] = mark.get("source_timestamp") if mark else None
            enriched.append(item)
        watchlist = list(DEFAULT_WATCHLIST)
        watch_marks = self.ledger.latest_marks(watchlist)
        return {
            "paper_only": True,
            "settings": settings,
            "market_data": self.market_data.connection_status(),
            "quote_refresh_seconds": self.quote_stale_seconds,
            "portfolio": current_mark,
            "positions": enriched,
            "watchlist_quotes": [
                watch_marks[symbol] for symbol in watchlist if symbol in watch_marks
            ],
            "orders": self.ledger.order_history(),
            "equity_history": self.ledger.equity_history(),
            "strategies": self.strategies(),
            "events": self.ledger.events(),
            "server_time": datetime.now(UTC).isoformat(),
        }
