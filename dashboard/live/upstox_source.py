"""Real Upstox market-data adapter for the live terminal (READ-ONLY).

This is the **single file** that talks to Upstox.  The rest of the live
terminal consumes what this class provides (LIVE mode) or falls back to the
clearly-labelled SIM feed when it is unavailable.  It has no order, funds,
portfolio or GTT methods — by construction it cannot move real money.

Requirements: ``upstox-python-sdk`` (see pyproject.toml) and a valid daily
OAuth access token in ``UPSTOX_ACCESS_TOKEN`` (or
``UPSTOX_SANDBOX_ACCESS_TOKEN`` for sandbox).

API shape reference: ``.agents/skills/upstox/references/market-data.md``
(verified against the SDK by this repository's own Upstox skill).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

logger = logging.getLogger(__name__)


class RealFeedUnavailable(RuntimeError):
    """Raised when the Upstox source cannot be constructed or has no token."""


@dataclass(frozen=True, slots=True)
class RealQuote:
    """One instrument's live quote, normalised for the terminal."""

    symbol: str
    last: float
    bid: float | None
    ask: float | None
    day_open: float | None
    day_high: float | None
    day_low: float | None
    prev_close: float | None
    volume: float | None
    timestamp_ms: int


def _val(source: Any, *names: str) -> Any:
    """Read a field from an SDK object or a dict-like payload."""
    for name in names:
        if isinstance(source, Mapping) and name in source:
            return source[name]
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _parse_candle_time(value: Any) -> int | None:
    """Parse Upstox candle timestamps (``YYYY-MM-DDTHH:MM:SS[.ffffff]``) to ms (IST)."""
    if isinstance(value, (int, float)):
        seconds = float(value) / (1000.0 if float(value) > 10_000_000_000 else 1.0)
        return int(seconds * 1000)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            # bare IST wall time, as Upstox commonly returns
            try:
                parsed = datetime.fromisoformat(value).replace(tzinfo=IST)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return int(parsed.timestamp() * 1000)
    return None


class UpstoxLiveSource:
    """Read-only Upstox feed: full quotes + v3 historical/intraday candles."""

    QUOTE_POLL_SECONDS = 10.0
    WARMUP_REQUEST_GAP_SECONDS = 0.25
    HISTORY_MINUTES_DAYS = 5
    DAILY_HISTORY_DAYS = 400

    def __init__(
        self,
        token: str,
        instruments: Mapping[str, str],
        *,
        sandbox: bool = False,
        unmapped: list[str] | None = None,
    ) -> None:
        self.token = (token or "").strip()
        if not self.token:
            raise RealFeedUnavailable("no Upstox access token configured")
        if not instruments:
            raise RealFeedUnavailable("no verified instrument-key mapping available")
        self._instruments = {str(k): str(v) for k, v in instruments.items() if v}
        if not self._instruments:
            raise RealFeedUnavailable("no verified instrument-key mapping available")
        self._sandbox = sandbox
        try:
            import upstox_client
        except ImportError as exc:
            raise RealFeedUnavailable(
                "upstox-python-sdk is not installed (pip install upstox-python-sdk)"
            ) from exc
        self._sdk = upstox_client
        try:
            configuration = upstox_client.Configuration(sandbox=sandbox)
            configuration.access_token = self.token
            client = upstox_client.ApiClient(configuration)
            self._quote_api = upstox_client.MarketQuoteApi(client)
            self._hist_api = upstox_client.HistoryV3Api(client)
            self._mkt_api = upstox_client.MarketHolidaysAndTimingsApi(client)
        except Exception as exc:  # SDK misconfiguration
            raise RealFeedUnavailable(f"Upstox client init failed: {exc}") from exc
        # state
        self.lock = threading.Lock()
        self.warmed = False
        self.healthy = True
        self.error: str | None = None
        self.market_status: str | None = None
        self.last_success_at: str | None = None
        self.last_latency_ms: float | None = None
        self.real_symbols: list[str] = []
        self.sim_fallback_symbols: list[str] = []
        # requested symbols that have no verified Upstox instrument key at all;
        # the feed keeps them on the labelled SIM walk so prices never freeze
        self.unmapped_symbols: list[str] = [str(s) for s in (unmapped or [])]
        self._one_m_history: dict[str, list[list[float]]] = {}
        self._daily_history: dict[str, list[list[float]]] = {}
        self._last_intraday_refresh: dict[str, float] = {}
        self._stop = threading.Event()

    # -- introspection -------------------------------------------------------

    @property
    def token_variable(self) -> str:
        return "UPSTOX_SANDBOX_ACCESS_TOKEN" if self._sandbox else "UPSTOX_ACCESS_TOKEN"

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "configured": True,
                "healthy": self.healthy,
                "warmed": self.warmed,
                "market_status": self.market_status,
                "last_success_at": self.last_success_at,
                "last_latency_ms": round(self.last_latency_ms, 1)
                if self.last_latency_ms
                else None,
                "error": self.error,
                "real_symbols": list(self.real_symbols),
                "sim_fallback_symbols": list(self.sim_fallback_symbols),
                "unmapped_symbols": list(self.unmapped_symbols),
            }

    def _note(self, ok: bool, error: str | None = None) -> None:
        with self.lock:
            if ok:
                self.healthy = True
                self.error = None
                self.last_success_at = datetime.now(UTC).isoformat()
            else:
                self.healthy = False
                self.error = error
                logger.warning("upstox_live_source_error: %s", error)

    # -- warm-up -------------------------------------------------------------

    def warm(self, symbols: list[str]) -> None:
        """Fetch market status + 1-minute and daily history for each symbol.

        Per-symbol failures degrade that symbol to the SIM fallback instead of
        failing the whole feed.  Safe to run from a background thread.
        """
        started = time.monotonic()
        try:
            self._market_status()
        except Exception as exc:  # noqa: BLE001 — status is informational
            self._note(False, f"market status: {exc}")
        got_any = False
        for symbol in symbols:
            if self._stop.is_set():
                break
            if symbol not in self._instruments:
                continue
            one_m = self._fetch_one_m(symbol)
            daily = self._fetch_daily(symbol)
            if one_m is not None or daily is not None:
                got_any = True
                with self.lock:
                    if one_m is not None:
                        self._one_m_history[symbol] = one_m
                        if symbol not in self.real_symbols:
                            self.real_symbols.append(symbol)
                    if daily is not None:
                        self._daily_history[symbol] = daily
            else:
                with self.lock:
                    if symbol not in self.sim_fallback_symbols:
                        self.sim_fallback_symbols.append(symbol)
            time.sleep(self.WARMUP_REQUEST_GAP_SECONDS)
        with self.lock:
            self.warmed = got_any
        self._note(got_any, None if got_any else "no candle history could be fetched")
        logger.info(
            "upstox_live_source_warmed real=%s sim_fallback=%s in %.1fs",
            len(self.real_symbols),
            len(self.sim_fallback_symbols),
            time.monotonic() - started,
        )

    def stop(self) -> None:
        self._stop.set()

    def _market_status(self) -> bool:
        try:
            response = self._mkt_api.get_market_status(exchange="NSE")
            data = _val(response, "data")
            status = _val(data, "status")
            if status is None and isinstance(data, Mapping):
                status = data.get("NSE")
            with self.lock:
                self.market_status = str(status or "UNKNOWN").upper()
            return self.market_status in {
                "OPEN",
                "CLOSED",
                "PRE_OPEN",
                "POST_CLOSE",
                "HOLIDAY",
            }
        except Exception:
            with self.lock:
                self.market_status = "UNKNOWN"
            return False

    # -- candles -------------------------------------------------------------

    def _candles_to_bars(self, candles: Any) -> list[list[float]] | None:
        """Normalise SDK candle rows to ascending [[t_ms,o,h,l,c,v], ...]."""
        rows = _val(candles, "candles") or candles
        if not isinstance(rows, (list, tuple)) or not rows:
            return None
        out: list[list[float]] = []
        for row in rows:
            try:
                ts = _parse_candle_time(row[0])
                o, h, lo, c = (
                    float(row[1]),
                    float(row[2]),
                    float(row[3]),
                    float(row[4]),
                )
                v = float(row[5]) if len(row) > 5 else 0.0
            except (IndexError, TypeError, ValueError):
                continue
            if ts is None or min(o, h, lo, c) <= 0:
                continue
            out.append([ts, o, h, lo, c, v])
        if not out:
            return None
        out.sort(key=lambda bar: bar[0])
        return out

    def fetch_1m_history(
        self, symbol: str, days: int = HISTORY_MINUTES_DAYS
    ) -> list[list[float]] | None:
        key = self._instruments.get(symbol)
        if not key:
            return None
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=days)).isoformat()
        started = time.monotonic()
        try:
            response = self._hist_api.get_historical_candle_data1(
                key, "minutes", "1", to_date, from_date
            )
            bars = self._candles_to_bars(_val(response, "data"))
            self._note(bars is not None, None if bars else "empty 1-minute history")
            if self.last_latency_ms is None or True:
                with self.lock:
                    self.last_latency_ms = (time.monotonic() - started) * 1000.0
            return bars
        except Exception as exc:  # noqa: BLE001
            logger.warning("1-minute history for %s: %s", symbol, exc)
            return None

    def _fetch_one_m(self, symbol: str) -> list[list[float]] | None:
        bars = self.fetch_1m_history(symbol)
        return bars

    def fetch_daily_history(
        self, symbol: str, days: int = DAILY_HISTORY_DAYS
    ) -> list[list[float]] | None:
        key = self._instruments.get(symbol)
        if not key:
            return None
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=days)).isoformat()
        try:
            response = self._hist_api.get_historical_candle_data1(
                key, "days", "1", to_date, from_date
            )
            bars = self._candles_to_bars(_val(response, "data"))
            self._note(bars is not None, None if bars else "empty daily history")
            return bars
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily history for %s: %s", symbol, exc)
            return None

    def _fetch_daily(self, symbol: str) -> list[list[float]] | None:
        return self.fetch_daily_history(symbol)

    def refresh_intraday(
        self, symbol: str, min_gap_seconds: float = 90.0
    ) -> list[list[float]] | None:
        """Authoritative 1-minute bars for today.  Rate-limited per symbol."""
        now = time.monotonic()
        if now - self._last_intraday_refresh.get(symbol, 0.0) < min_gap_seconds:
            return None
        self._last_intraday_refresh[symbol] = now
        key = self._instruments.get(symbol)
        if not key:
            return None
        try:
            response = self._hist_api.get_intra_day_candle_data(key, "minutes", "1")
            bars = self._candles_to_bars(_val(response, "data"))
            if bars is None:
                # Empty is legitimate before the open (or on non-session days);
                # only count it as an error while the market is actually open.
                open_now = (self.market_status or "").upper() in {
                    "OPEN",
                    "PRE_OPEN",
                }
                if open_now:
                    logger.warning(
                        "empty intraday candles while market open for %s", symbol
                    )
            else:
                self._note(True)
            return bars
        except Exception as exc:  # noqa: BLE001
            logger.warning("intraday candles for %s: %s", symbol, exc)
            return None

    # -- quotes ----------------------------------------------------------------

    def fetch_quotes(self, symbols: list[str]) -> dict[str, RealQuote]:
        """Batch full market quotes (up to 500 instruments per call)."""
        wanted = [s for s in symbols if s in self._instruments]
        if not wanted:
            return {}
        started = time.monotonic()
        try:
            response = self._quote_api.get_full_market_quote(
                symbol=",".join(self._instruments[s] for s in wanted),
                api_version="2.0",
            )
            raw = _val(response, "data")
            if (
                isinstance(raw, Mapping)
                and "data" in raw
                and isinstance(raw["data"], Mapping)
            ):
                raw = raw["data"]
            if not isinstance(raw, Mapping):
                raise ValueError("quote response has no quote map")
            reverse = {v: k for k, v in self._instruments.items()}
            result: dict[str, RealQuote] = {}
            for instrument_key, quote in raw.items():
                instrument_token = str(
                    _val(quote, "instrument_token") or instrument_key
                )
                symbol = reverse.get(instrument_token)
                if symbol is None:
                    continue
                last = _num(_val(quote, "last_price", "ltp"))
                if last is None:
                    continue
                ohlc = _val(quote, "ohlc")
                depth = _val(quote, "depth")
                buys = _val(depth, "buy") if depth is not None else None
                sells = _val(depth, "sell") if depth is not None else None
                bid = (
                    _num(_val(buys[0], "price"))
                    if isinstance(buys, (list, tuple)) and buys
                    else _num(_val(quote, "bid_price"))
                )
                ask = (
                    _num(_val(sells[0], "price"))
                    if isinstance(sells, (list, tuple)) and sells
                    else _num(_val(quote, "ask_price"))
                )
                result[symbol] = RealQuote(
                    symbol=symbol,
                    last=last,
                    bid=bid,
                    ask=ask,
                    day_open=_num(_val(ohlc, "open")),
                    day_high=_num(_val(ohlc, "high")),
                    day_low=_num(_val(ohlc, "low")),
                    # v2 full quote: ohlc.close is the PREVIOUS day close
                    prev_close=_num(_val(ohlc, "close")),
                    volume=_num(_val(quote, "volume")),
                    timestamp_ms=int(time.time() * 1000),
                )
            self._note(
                bool(result),
                None if result else "no usable quotes in response",
            )
            with self.lock:
                self.last_latency_ms = (time.monotonic() - started) * 1000.0
            return result
        except Exception as exc:  # noqa: BLE001 — token expiry, network, rate limit
            self._note(False, f"full market quote: {exc}")
            return {}

    def one_m_history(self, symbol: str) -> list[list[float]] | None:
        with self.lock:
            return self._one_m_history.get(symbol)

    def daily_history(self, symbol: str) -> list[list[float]] | None:
        with self.lock:
            return self._daily_history.get(symbol)


def build_upstox_source(root: Any, symbols: dict[str, Any]) -> UpstoxLiveSource | None:
    """Construct the real source from the environment, or None.

    ``symbols`` maps display symbol → live-feed object; only symbols with a
    verified Upstox instrument key participate.
    """
    sandbox = False
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        sandbox = True
        token = os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN")
    if not token:
        return None
    try:
        from paper_trading.market_data import load_nifty_instruments

        mapping = load_nifty_instruments(root, index_name="nifty500")
    except Exception:  # noqa: BLE001
        mapping = {}
    instruments = {
        symbol: mapping[symbol]
        for symbol in symbols
        if symbol in mapping and not getattr(symbols[symbol], "is_index", False)
    }
    if "NIFTY_50" in symbols and "NIFTY_50" in mapping:
        instruments["NIFTY_50"] = mapping["NIFTY_50"]
    unmapped = [symbol for symbol in symbols if symbol not in instruments]
    try:
        return UpstoxLiveSource(token, instruments, sandbox=sandbox, unmapped=unmapped)
    except RealFeedUnavailable as exc:
        logger.info("upstox_live_source_unavailable: %s", exc)
        return None
