"""Read-only Upstox market-data adapter for local paper trading.

The adapter intentionally has no order-placement, portfolio, funds, or GTT
methods.  A valid Upstox access token grants this module the ability to read
quotes only; virtual orders are recorded exclusively in :mod:`paper_trading`
local SQLite state.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping


class MarketDataUnavailable(RuntimeError):
    """Raised when a read-only market-data snapshot cannot be obtained."""


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """A normalised read-only quote snapshot."""

    symbol: str
    instrument_key: str
    last_price: float
    bid_price: float | None
    ask_price: float | None
    volume: float | None
    timestamp: datetime
    source: str = "upstox"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["timestamp"] = self.timestamp.isoformat()
        return value


def _value(source: Any, *names: str) -> Any:
    """Read a field from either an SDK object or a dict-like response."""
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _positive_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_time(value: Any) -> datetime:
    """Best-effort source timestamp parser with a UTC receive-time fallback."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        # Upstox can return epoch milliseconds in some quote payloads.
        seconds = float(value) / (1000.0 if value > 10_000_000_000 else 1.0)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return datetime.now(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


class UpstoxMarketData:
    """Read full Upstox market quotes in batches of up to 500 instruments.

    ``sandbox`` merely chooses the SDK sandbox configuration/token variable;
    the object remains data-only in both modes.  App API key/secret credentials
    are insufficient by themselves: Upstox's daily OAuth access token is needed
    for a live quote request.
    """

    MAX_INSTRUMENTS_PER_REQUEST = 500

    def __init__(
        self,
        access_token: str | None = None,
        *,
        sandbox: bool = False,
    ) -> None:
        self.sandbox = bool(sandbox)
        self.access_token = (access_token or "").strip()

    @classmethod
    def from_environment(cls, sandbox: bool = False) -> "UpstoxMarketData":
        key = "UPSTOX_SANDBOX_ACCESS_TOKEN" if sandbox else "UPSTOX_ACCESS_TOKEN"
        return cls(os.getenv(key), sandbox=sandbox)

    @property
    def token_variable(self) -> str:
        return "UPSTOX_SANDBOX_ACCESS_TOKEN" if self.sandbox else "UPSTOX_ACCESS_TOKEN"

    def connection_status(self) -> dict[str, Any]:
        """Report configuration status without contacting Upstox or exposing secrets."""
        if not self.access_token:
            return {
                "configured": False,
                "mode": "SANDBOX" if self.sandbox else "UPSTOX_DATA",
                "detail": f"{self.token_variable} is not configured",
            }
        try:
            import upstox_client  # noqa: F401
        except ImportError:
            return {
                "configured": False,
                "mode": "SANDBOX" if self.sandbox else "UPSTOX_DATA",
                "detail": "upstox-python-sdk is not installed",
            }
        return {
            "configured": True,
            "mode": "SANDBOX" if self.sandbox else "UPSTOX_DATA",
            "detail": "token present; quote connection not yet checked",
        }

    def fetch_quotes(self, instruments: Mapping[str, str]) -> dict[str, MarketQuote]:
        """Return latest full quotes keyed by canonical symbol.

        Parameters map a display symbol (for example ``RELIANCE``) to an
        Upstox instrument key (for example ``NSE_EQ|INE002A01018``).  No quote
        is fabricated when the token, SDK, API response, or a price is absent.
        """
        if not instruments:
            return {}
        if not self.access_token:
            raise MarketDataUnavailable(
                f"configure {self.token_variable}; API key/secret alone cannot fetch quotes"
            )
        try:
            import upstox_client
            from upstox_client.rest import ApiException
        except ImportError as exc:
            raise MarketDataUnavailable(
                "upstox-python-sdk is required for live paper-market data"
            ) from exc

        reverse = {str(key): str(symbol).upper() for symbol, key in instruments.items()}
        result: dict[str, MarketQuote] = {}
        keys = list(reverse)
        try:
            configuration = upstox_client.Configuration(sandbox=self.sandbox)
            configuration.access_token = self.access_token
            api = upstox_client.MarketQuoteApi(upstox_client.ApiClient(configuration))
            for start in range(0, len(keys), self.MAX_INSTRUMENTS_PER_REQUEST):
                batch = keys[start : start + self.MAX_INSTRUMENTS_PER_REQUEST]
                response = api.get_full_market_quote(
                    symbol=",".join(batch), api_version="2.0"
                )
                raw = _value(response, "data")
                if isinstance(raw, Mapping) and "data" in raw:
                    raw = raw["data"]
                if not isinstance(raw, Mapping):
                    raise MarketDataUnavailable(
                        "Upstox quote response has no quote map"
                    )
                for instrument_key, quote in raw.items():
                    instrument_token = str(_value(quote, "instrument_token") or instrument_key)
                    symbol = reverse.get(instrument_token)
                    if symbol is None:
                        continue
                    last = _positive_or_none(_value(quote, "last_price", "ltp"))
                    if last is None:
                        continue
                    depth = _value(quote, "depth")
                    buys = _value(depth, "buy") if depth is not None else None
                    sells = _value(depth, "sell") if depth is not None else None
                    bid = (
                        _positive_or_none(_value(buys[0], "price"))
                        if isinstance(buys, (list, tuple)) and buys
                        else _positive_or_none(_value(quote, "bid_price"))
                    )
                    ask = (
                        _positive_or_none(_value(sells[0], "price"))
                        if isinstance(sells, (list, tuple)) and sells
                        else _positive_or_none(_value(quote, "ask_price"))
                    )
                    result[symbol] = MarketQuote(
                        symbol=symbol,
                        instrument_key=str(instrument_key),
                        last_price=last,
                        bid_price=bid,
                        ask_price=ask,
                        volume=_positive_or_none(_value(quote, "volume")),
                        timestamp=_parse_time(
                            _value(quote, "last_trade_time", "timestamp")
                        ),
                    )
        except ApiException as exc:
            raise MarketDataUnavailable("Upstox quote request failed") from exc
        except MarketDataUnavailable:
            raise
        except (
            Exception
        ) as exc:  # SDK transport errors are intentionally hidden from UI.
            raise MarketDataUnavailable("Upstox quote request failed") from exc
        return result


def load_nifty_instruments(
    root: Path | str = ".",
    *,
    index_name: str = "nifty50",
    limit: int | None = None,
) -> dict[str, str]:
    """Build a verified symbol → Upstox instrument-key map from local PIT data.

    Entries with malformed/absent ISINs are skipped rather than guessed.  The
    Nifty 50 index itself is included as a quote-only benchmark.
    """
    import csv

    path = Path(root) / "data" / "universe" / f"{index_name}-pit" / f"{index_name}.csv"
    if not path.is_file():
        return {"NIFTY_50": "NSE_INDEX|Nifty 50"}
    records: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol") or "").strip().upper()
            isin = str(row.get("isin") or "").strip().upper()
            if not symbol or not isin.startswith("INE"):
                continue
            records[symbol] = f"NSE_EQ|{isin}"
            if limit and len(records) >= limit:
                break
    records["NIFTY_50"] = "NSE_INDEX|Nifty 50"
    return records
