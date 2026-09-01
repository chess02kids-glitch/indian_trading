"""Quote provider chain: UPSTOX -> SIM -> EOD, always honestly labelled.

Why this exists
---------------
The paper account used to call Upstox directly.  With no ``UPSTOX_ACCESS_TOKEN``
in the environment that call raised, the ledger recorded a permanent
``last_quote_error``, the quote tape showed ``ERROR`` / ``UNAVAILABLE`` on every
single load, and the five default watchlist symbols never got a price — even
though the Live Terminal next door was rendering a full simulated tape from the
very same repository data.

The chain below makes the degradation explicit instead of fatal:

``UPSTOX``   real read-only quotes (token + SDK present).  The only source that
             may ever be labelled LIVE.
``SIM``      a clearly-labelled simulated walk anchored to the last verified EOD
             close, so the account can still be marked to market and exercised
             end to end.  Every quote carries ``source="SIM"``.
``EOD``      the last verified close, frozen.  Used when even the simulator has
             no anchor (brand-new symbol).

A quote is never silently promoted: :class:`QuoteChain` reports how many symbols
came from each source, and callers surface that verbatim.
"""

from __future__ import annotations

import logging
import math
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
SESSION_OPEN_MIN = 9 * 60 + 15  # 09:15 IST
SESSION_CLOSE_MIN = 15 * 60 + 30  # 15:30 IST
DEFAULT_SIM_SPREAD_BPS = 6.0  # half-spread each side, in basis points


def _now_utc() -> datetime:
    return datetime.now(UTC)


#: Benchmark indices are not equities, so they are absent from the price panel.
#: They live in the raw EOD mirror under a spaced filename ("nifty 50.csv").
INDEX_FILES = {
    "NIFTY_50": "nifty 50.csv",
    "NIFTY_BANK": "nifty bank.csv",
    "NIFTY_100": "nifty 100.csv",
    "NIFTY_500": "nifty 500.csv",
}

_INDEX_CACHE: dict[str, tuple[float, float]] = {}
_INDEX_CACHE_AT: datetime | None = None


def _raw_eod_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data" / "eod2" / "daily"


def index_anchors() -> dict[str, tuple[float, float]]:
    """``symbol -> (last_close, annualised_sigma)`` for the benchmark indices."""
    global _INDEX_CACHE, _INDEX_CACHE_AT
    now = _now_utc()
    if _INDEX_CACHE_AT is not None and (now - _INDEX_CACHE_AT) < timedelta(minutes=30):
        return dict(_INDEX_CACHE)
    out: dict[str, tuple[float, float]] = {}
    for symbol, filename in INDEX_FILES.items():
        path = _raw_eod_dir() / filename
        if not path.is_file():
            continue
        try:
            import numpy as np
            import pandas as pd

            frame = pd.read_csv(path)
            frame.columns = [str(c).strip().lower() for c in frame.columns]
            close = pd.to_numeric(frame["close"], errors="coerce").dropna()
            if len(close) < 30:
                continue
            sigma = float(close.pct_change().std() * np.sqrt(252.0))
            out[symbol] = (float(close.iloc[-1]), max(0.004, min(0.12, sigma or 0.015)))
        except Exception as exc:  # noqa: BLE001 - an index is a nicety, never fatal
            logger.debug("index_anchor_failed %s: %s", symbol, exc)
    _INDEX_CACHE = out
    _INDEX_CACHE_AT = now
    return dict(out)


def _session_progress(now: datetime) -> tuple[bool, float]:
    """Return ``(is_open, fraction_of_session_elapsed)`` for NSE cash hours."""
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        return False, 0.0
    minutes = local.hour * 60 + local.minute
    if minutes < SESSION_OPEN_MIN:
        return False, 0.0
    if minutes >= SESSION_CLOSE_MIN:
        return True, 1.0
    elapsed = (minutes - SESSION_OPEN_MIN) / (SESSION_CLOSE_MIN - SESSION_OPEN_MIN)
    return True, max(0.0, min(1.0, elapsed))


@dataclass(slots=True)
class QuoteResult:
    """One normalised quote with its provenance attached."""

    symbol: str
    last_price: float
    source: str  # "UPSTOX" | "SIM" | "EOD"
    bid_price: float | None = None
    ask_price: float | None = None
    volume: float | None = None
    prev_close: float | None = None
    source_timestamp: datetime = field(default_factory=_now_utc)
    note: str = ""

    @property
    def change_pct(self) -> float | None:
        if not self.prev_close:
            return None
        return (self.last_price / self.prev_close - 1.0) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "instrument_key": f"SIM|{self.symbol}" if self.source != "UPSTOX" else self.symbol,
            "last_price": round(float(self.last_price), 4),
            "bid_price": None if self.bid_price is None else round(float(self.bid_price), 4),
            "ask_price": None if self.ask_price is None else round(float(self.ask_price), 4),
            "volume": None if self.volume is None else float(self.volume),
            "timestamp": self.source_timestamp.isoformat(),
            "source": self.source,
            "prev_close": None if self.prev_close is None else round(float(self.prev_close), 4),
            "change_pct": None if self.change_pct is None else round(self.change_pct, 3),
            "note": self.note,
        }

    def as_market_quote_dict(self) -> dict[str, Any]:
        """Shape expected by :meth:`paper_trading.ledger.PaperLedger.record_marks`."""
        return {
            "symbol": self.symbol,
            "instrument_key": f"{self.source}|{self.symbol}",
            "last_price": float(self.last_price),
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "volume": self.volume,
            "timestamp": self.source_timestamp.isoformat(),
            "source": self.source.lower(),
        }


class QuoteProvider:
    """Base class for one rung of the quote chain."""

    name = "BASE"
    label = "unknown"

    def available(self) -> bool:  # pragma: no cover - overridden
        return False

    def status(self) -> dict[str, Any]:  # pragma: no cover - overridden
        return {"name": self.name, "available": False}

    def fetch(self, symbols: Sequence[str]) -> dict[str, QuoteResult]:  # pragma: no cover
        return {}


# Tie-break when two sources served an equal number of quotes: prefer the more
# trustworthy label, so a tie is never resolved *down* to a weaker source.
_SOURCE_RANK = {"UPSTOX": 0, "EOD": 1, "SIM": 2}


class UpstoxQuoteProvider(QuoteProvider):
    """Read-only Upstox quotes via :mod:`paper_trading.market_data`."""

    name = "UPSTOX"
    label = "Upstox read-only quotes"

    def __init__(self, market_data: Any, instruments: Mapping[str, str] | None = None) -> None:
        self.market_data = market_data
        self._instruments: dict[str, str] = dict(instruments or {})
        self._detail = ""

    def available(self) -> bool:
        """True when this client can actually serve quotes.

        A token is the usual signal, but the client itself is the authority: an
        ``UpstoxMarketData`` with no access token reports
        ``{"configured": False}``, while any properly configured read-only
        client reports ``{"configured": True}``.  Honouring that keeps the
        UPSTOX -> SIM -> EOD degradation working for real credentials *and*
        lets a test double stand in for the broker without faking a token.
        """
        token = getattr(self.market_data, "access_token", "") or ""
        if str(token).strip():
            return True
        try:
            detail = self.market_data.connection_status()
        except Exception:  # noqa: BLE001 - an unavailable client is just unavailable
            return False
        return bool(isinstance(detail, Mapping) and detail.get("configured"))

    def status(self) -> dict[str, Any]:
        try:
            detail = self.market_data.connection_status()
        except Exception as exc:  # noqa: BLE001 - never leak an SDK failure
            detail = {"configured": False, "detail": str(exc)}
        return {
            "name": self.name,
            "label": self.label,
            "available": self.available(),
            "instruments_mapped": len(self._instruments),
            **{k: v for k, v in detail.items() if k != "configured"},
        }

    def fetch(self, symbols: Sequence[str]) -> dict[str, QuoteResult]:
        if not self.available():
            return {}
        mapped = {
            symbol: self._instruments[symbol]
            for symbol in symbols
            if symbol in self._instruments
        }
        if not mapped:
            self._detail = "no verified Upstox instrument key for the requested symbols"
            return {}
        try:
            raw = self.market_data.fetch_quotes(mapped)
        except Exception as exc:  # noqa: BLE001 - a broker failure must not kill the page
            self._detail = str(exc)
            logger.warning("upstox_quote_fetch_failed: %s", exc)
            return {}
        out: dict[str, QuoteResult] = {}
        for symbol, quote in raw.items():
            out[str(symbol).upper()] = QuoteResult(
                symbol=str(symbol).upper(),
                last_price=float(quote.last_price),
                source=self.name,
                bid_price=quote.bid_price,
                ask_price=quote.ask_price,
                volume=quote.volume,
                source_timestamp=quote.timestamp,
                note="Upstox read-only market data",
            )
        self._detail = f"{len(out)}/{len(mapped)} symbols quoted"
        return out


@dataclass
class _SimState:
    """Per-symbol simulated intraday walk, anchored to a verified EOD close."""

    anchor_close: float
    sigma_daily: float
    session_day: str = ""
    price: float = 0.0
    last_tick: datetime | None = None
    volume: float = 0.0
    rng: Any = None


class SimQuoteProvider(QuoteProvider):
    """Deterministic-per-session simulated quotes anchored to real EOD closes.

    The walk advances only while the NSE cash session is open, so the tape
    behaves like a market (frozen outside 09:15–15:30 IST) without pretending
    to be one.  Volatility is the symbol's own realised daily sigma.
    """

    name = "SIM"
    label = "Simulated quotes from verified EOD history"

    def __init__(self, vol_boost: float = 1.0, spread_bps: float = DEFAULT_SIM_SPREAD_BPS) -> None:
        self.vol_boost = float(vol_boost)
        self.spread_bps = float(spread_bps)
        self._state: dict[str, _SimState] = {}
        self._anchor_cache: dict[str, tuple[float, float]] = {}
        self._anchor_cache_at: datetime | None = None

    def available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": True,
            "symbols_tracked": len(self._state),
            "vol_boost": self.vol_boost,
            "half_spread_bps": self.spread_bps,
            "detail": "anchored to the last verified EOD close; advances during NSE hours only",
        }

    # -- anchors ------------------------------------------------------------

    def _anchors(self, symbols: Iterable[str]) -> dict[str, tuple[float, float]]:
        """Return ``symbol -> (last_close, sigma_daily)`` from the shared panel."""
        now = _now_utc()
        if self._anchor_cache_at is not None and (now - self._anchor_cache_at) < timedelta(
            minutes=5
        ):
            return {s: self._anchor_cache[s] for s in symbols if s in self._anchor_cache}
        try:
            import numpy as np
            import pandas as pd

            from datahub.panel import wide

            close = wide("close")
            rets = close.pct_change(fill_method=None)
            sigma = (rets.std() * np.sqrt(252.0)).fillna(0.018)
            last = close.ffill().iloc[-1]
            self._anchor_cache = {
                str(sym): (float(last[sym]), float(max(0.004, min(0.12, sigma[sym] or 0.018))))
                for sym in close.columns
                if not pd.isna(last[sym])
            }
            self._anchor_cache_at = now
        except Exception as exc:  # noqa: BLE001 - fall back to the caller's hint
            logger.warning("sim_anchor_load_failed: %s", exc)
        anchors = {s: self._anchor_cache[s] for s in symbols if s in self._anchor_cache}
        for symbol in symbols:
            if symbol not in anchors and symbol in index_anchors():
                anchors[symbol] = index_anchors()[symbol]
        return anchors

    # -- walk ---------------------------------------------------------------

    def _advance(self, state: _SimState, now: datetime, anchor: float) -> None:
        today = now.astimezone(IST).date().isoformat()
        if state.session_day != today:
            state.session_day = today
            state.price = anchor
            state.anchor_close = anchor
            state.last_tick = now
            state.volume = 0.0
            state.rng = random.Random(f"{state.anchor_close}|{today}|{id(state)}")  # nosec B311
            return
        is_open, _progress = _session_progress(now)
        if not is_open:
            state.last_tick = now
            return
        if state.last_tick is None:
            state.last_tick = now
        elapsed_seconds = max(0.0, (now - state.last_tick).total_seconds())
        state.last_tick = now
        # scale a daily sigma to the elapsed wall-clock slice of the session
        seconds_in_session = (SESSION_CLOSE_MIN - SESSION_OPEN_MIN) * 60.0
        sigma_slice = state.sigma_daily * math.sqrt(elapsed_seconds / seconds_in_session)
        sigma_slice *= self.vol_boost
        if sigma_slice > 0:
            state.price = max(0.01, state.price * (1.0 + state.rng.gauss(0.0, sigma_slice)))
        state.volume += max(0.0, elapsed_seconds) * 1_200.0 * (0.5 + state.rng.random())

    def fetch(self, symbols: Sequence[str]) -> dict[str, QuoteResult]:
        anchors = self._anchors(symbols)
        now = _now_utc()
        out: dict[str, QuoteResult] = {}
        half = self.spread_bps / 10_000.0
        for symbol in symbols:
            anchor = anchors.get(symbol)
            if anchor is None:
                continue
            close, sigma = anchor
            state = self._state.get(symbol)
            if state is None:
                state = _SimState(anchor_close=close, sigma_daily=sigma)
                self._state[symbol] = state
            if state.anchor_close <= 0:
                state.anchor_close = close
                state.sigma_daily = sigma
            self._advance(state, now, close)
            price = state.price
            out[symbol] = QuoteResult(
                symbol=symbol,
                last_price=price,
                source=self.name,
                bid_price=price * (1.0 - half),
                ask_price=price * (1.0 + half),
                volume=state.volume,
                prev_close=state.anchor_close,
                source_timestamp=now,
                note="simulated from verified EOD history — not a broker quote",
            )
        return out


class EodQuoteProvider(QuoteProvider):
    """Last verified EOD close, frozen.  Never presented as a live price."""

    name = "EOD"
    label = "Last verified EOD close (frozen)"

    def available(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "available": True,
            "detail": "frozen last close; used only when no fresher source exists",
        }

    def fetch(self, symbols: Sequence[str]) -> dict[str, QuoteResult]:
        try:
            import pandas as pd

            from datahub.panel import wide

            close = wide("close")
            last = close.ffill().iloc[-1]
            as_of = close.dropna(how="all").index[-1]
        except Exception as exc:  # noqa: BLE001
            logger.warning("eod_quote_load_failed: %s", exc)
            return {}
        out: dict[str, QuoteResult] = {}
        indices = index_anchors()
        for symbol in symbols:
            value = last.get(symbol)
            if value is None or pd.isna(value):
                if symbol in indices:
                    value = indices[symbol][0]
                else:
                    continue
            out[symbol] = QuoteResult(
                symbol=symbol,
                last_price=float(value),
                source=self.name,
                source_timestamp=pd.Timestamp(as_of).to_pydatetime().replace(tzinfo=UTC),
                prev_close=float(value),
                note=f"last verified close on {pd.Timestamp(as_of).date()}",
            )
        return out


class QuoteChain:
    """Try each provider in order and merge, keeping the best source per symbol."""

    def __init__(self, providers: Sequence[QuoteProvider]) -> None:
        self.providers = list(providers)
        self.last_error: str | None = None

    @property
    def primary_source(self) -> str:
        for provider in self.providers:
            if provider.available():
                return provider.name
        return "NONE"

    def status(self) -> dict[str, Any]:
        return {
            "primary_source": self.primary_source,
            "last_error": self.last_error,
            "providers": [p.status() for p in self.providers],
        }

    def fetch(self, symbols: Sequence[str]) -> dict[str, QuoteResult]:
        wanted = [str(s).upper() for s in dict.fromkeys(symbols)]
        merged: dict[str, QuoteResult] = {}
        for provider in self.providers:
            missing = [s for s in wanted if s not in merged]
            if not missing or not provider.available():
                continue
            try:
                got = provider.fetch(missing)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{provider.name}: {exc}"
                logger.warning("quote_provider_failed %s: %s", provider.name, exc)
                continue
            merged.update(got)
        # a real broker error is only reported if nothing at all could be quoted
        if merged and self.providers:
            upstox = next((p for p in self.providers if p.name == "UPSTOX"), None)
            if upstox is not None and not upstox.available():
                self.last_error = None
        return merged

    def summarise(self, quotes: Mapping[str, QuoteResult]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for quote in quotes.values():
            counts[quote.source] = counts.get(quote.source, 0) + 1
        # Report the source that actually served the most quotes.  The previous
        # version picked by preference order (UPSTOX before SIM before EOD), so
        # a single real quote alongside four simulated ones labelled the whole
        # refresh "UPSTOX" — exactly the "never present a simulated price as a
        # real quote" failure this repository forbids.
        primary = "NONE"
        if counts:
            primary = max(
                counts, key=lambda name: (counts[name], -_SOURCE_RANK.get(name, 99))
            )
        mixed = len(counts) > 1
        return {
            "source": primary,
            "counts": counts,
            "quoted": len(quotes),
            "mixed": mixed,
            "sources": sorted(counts),
            "note": {
                "UPSTOX": "live read-only Upstox quotes",
                "SIM": "simulated quotes anchored to verified EOD closes — not real prices",
                "EOD": "frozen last verified close — market data is stale",
                "NONE": "no quote source could price any requested symbol",
            }.get(primary, "")
            + (
                " — MIXED SOURCES: only some symbols were priced by the primary source"
                if mixed
                else ""
            ),
        }


def build_quote_chain(
    market_data: Any,
    instruments: Mapping[str, str] | None = None,
    *,
    allow_sim: bool | None = None,
) -> QuoteChain:
    """Build the default UPSTOX -> SIM -> EOD chain.

    ``allow_sim=False`` disables the simulator (used by tests that assert no
    quote is fabricated); the chain then degrades to EOD only.
    """
    if allow_sim is None:
        allow_sim = os.getenv("QUANT_ALLOW_SIM_QUOTES", "1") not in ("0", "false", "False")
    providers: list[QuoteProvider] = [UpstoxQuoteProvider(market_data, instruments)]
    if allow_sim:
        providers.append(SimQuoteProvider())
    providers.append(EodQuoteProvider())
    return QuoteChain(providers)
