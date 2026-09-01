"""Live-terminal feed engine.

Builds a continuous, clearly-labelled **simulated** intraday market on top of
the verified EOD history in ``data/eod2/daily`` and drives a demo AI
paper-trader against the same local virtual ledger used by the paper
dashboard.  Safety boundary (unchanged from the rest of the repo):

* No broker order API is imported or reachable from this module.
* Virtual fills are written only to the local SQLite ledger
  (``var/paper_trading.sqlite``) via :class:`paper_trading.ledger.PaperLedger`.
* Every API response labels the feed as ``SIM`` — no simulated price is ever
  presented as a real Upstox quote.
"""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import queue
import random
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from paper_trading.ledger import PaperLedger

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

SESSION_MINUTES = 390  # NSE cash session 09:15–15:30 IST
SESSION_OPEN_MIN = 9 * 60 + 15  # 09:15
TICK_SECONDS = 0.25
VOL_BOOST = 1.6  # demo readability multiplier on intraday volatility
DEMO_HISTORY_DAYS = 12  # reconstructed intraday days behind the live day
MAX_BARS = 8_192
BOT_STRATEGY_ID = "ai_demo"

# (symbol, eod2 filename, display name, is_index)
UNIVERSE: tuple[tuple[str, str, str, bool], ...] = (
    ("NIFTY_50", "nifty 50.csv", "NIFTY 50 Index", True),
    ("RELIANCE", "reliance.csv", "Reliance Industries", False),
    ("TCS", "tcs.csv", "Tata Consultancy Services", False),
    ("HDFCBANK", "hdfcbank.csv", "HDFC Bank", False),
    ("ICICIBANK", "icicibank.csv", "ICICI Bank", False),
    ("INFOSYS", "infosys.csv", "Infosys", False),
    ("SBIN", "sbin.csv", "State Bank of India", False),
    ("ITC", "itc.csv", "ITC", False),
    ("BHARTIARTL", "bhartiartl.csv", "Bharti Airtel", False),
    ("KOTAKBANK", "kotakbank.csv", "Kotak Mahindra Bank", False),
    ("HCLTECH", "hcltech.csv", "HCL Technologies", False),
    ("LT", "lt.csv", "Larsen & Toubro", False),
    ("AXISBANK", "axisbank.csv", "Axis Bank", False),
    ("MARUTI", "maruti.csv", "Maruti Suzuki", False),
    ("TITAN", "titan.csv", "Titan", False),
    ("SUNPHARMA", "sunpharma.csv", "Sun Pharmaceutical", False),
    ("COALINDIA", "coalindia.csv", "Coal India", False),
    ("NESTLEIND", "nestleind.csv", "Nestle India", False),
    ("POWERGRID", "powergrid.csv", "Power Grid Corp", False),
    ("TATASTEEL", "tatasteel.csv", "Tata Steel", False),
    ("VEDL", "vedl.csv", "Vedanta", False),
    ("HAL", "hal.csv", "Hindustan Aeronautics", False),
    ("HDFCLIFE", "hdfclife.csv", "HDFC Life Insurance", False),
)

INTERVALS: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "1d": 1440,
    "1w": 10080,
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).isoformat()


def _u_shape_volume_weight(i: int) -> float:
    """U-shaped intraday volume profile (heaviest at open and close)."""
    n = SESSION_MINUTES
    return 1.0 + 1.4 * math.exp(-i / 45.0) + 0.9 * math.exp(-(n - 1 - i) / 70.0)


# ---------------------------------------------------------------------------
# Per-symbol feed
# ---------------------------------------------------------------------------


class SymbolFeed:
    """One instrument: real EOD history + deterministic reconstructed
    intraday days + the live simulated session."""

    def __init__(
        self, symbol: str, filename: str, name: str, is_index: bool, eod_dir: Path
    ) -> None:
        self.symbol = symbol
        self.name = name
        self.is_index = is_index
        self.daily: list[dict[str, Any]] = []
        self.last_eod_date: str | None = None
        self.prev_close: float = 0.0
        self.sigma_daily: float = 0.012
        self.base_min_volume: float = 1000.0
        path = eod_dir / filename
        if path.is_file():
            self._load_daily(path)
        # simulation state
        self.rng = random.Random(f"{symbol}|live|{int(time.time())}")  # nosec B311 - simulation, not security
        self.bars: list[dict[str, Any]] = []  # 1-minute bars {t,o,h,l,c,v,d,m}
        self.last_price: float = 0.0
        self.session_open: float = 0.0
        self.session_high: float = 0.0
        self.session_low: float = 1e18
        self.day_volume: float = 0.0
        self.vwap: float = 0.0
        self._cum_tp_vol: float = 0.0
        self._cum_vol: float = 0.0
        self._drift: float = 0.0
        self._drift_until: float = 0.0
        self._cur_bar: dict[str, Any] | None = None
        self._day_idx: int = DEMO_HISTORY_DAYS
        self._minute: int = 0
        self._session_day: str | None = None
        # warmed-up indicators (incremental)
        self.ema_fast: float | None = None
        self.ema_slow: float | None = None
        self.ema_fast_prev: float | None = None
        self.ema_slow_prev: float | None = None
        self._ema_fast_buf: list[float] = []
        self._ema_slow_buf: list[float] = []
        self._atr_sum: float = 0.0
        self._atr_count: int = 0
        self._prev_close_bar: float | None = None
        self.atr: float = 0.0
        self._rsi_ave_gain: float = 0.0
        self._rsi_ave_loss: float = 0.0
        self._rsi_gain_sum: float = 0.0
        self._rsi_loss_sum: float = 0.0
        self._rsi_count: int = 0
        self.rsi: float = 50.0

    # -- history ------------------------------------------------------------

    def _load_daily(self, path: Path) -> None:
        rows: list[dict[str, float]] = []
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames:
                    return
                key = {str(k).strip().lower(): k for k in reader.fieldnames}
                need = ("date", "open", "high", "low", "close", "volume")
                if any(name not in key for name in need):
                    return
                for row in reader:
                    try:
                        o = float(row[key["open"]])
                        h = float(row[key["high"]])
                        lo = float(row[key["low"]])
                        c = float(row[key["close"]])
                        v = float(row[key["volume"]] or 0)
                    except (TypeError, ValueError):
                        continue
                    if min(o, h, lo, c) <= 0:
                        continue
                    rows.append(
                        {
                            "date": str(row[key["date"]]).strip()[:10],
                            "open": o,
                            "high": h,
                            "low": lo,
                            "close": c,
                            "volume": v,
                        }
                    )
        except (OSError, csv.Error):
            return
        if len(rows) < 25:
            return
        self.daily = rows[-550:]
        last = self.daily[-1]
        self.last_eod_date = last["date"]
        self.prev_close = float(last["close"])
        closes = [r["close"] for r in self.daily]
        returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ][:120]
        if len(returns) >= 20:
            self.sigma_daily = min(0.06, max(0.004, statistics.pstdev(returns)))
        volumes = [r["volume"] for r in self.daily[-30:] if r["volume"] > 0]
        if volumes:
            self.base_min_volume = max(
                1.0, statistics.median(volumes) / SESSION_MINUTES
            )

    @property
    def ready(self) -> bool:
        return self.prev_close > 0

    def _session_start_ms(self, day_str: str) -> int:
        try:
            day = datetime.fromisoformat(day_str)
        except ValueError:
            return _now_ms()
        start = day.replace(hour=9, minute=15, second=0, microsecond=0, tzinfo=IST)
        return int(start.timestamp() * 1000)

    # -- deterministic reconstructed intraday day ----------------------------

    def _reconstruct_day(self, day: dict[str, Any]) -> list[dict[str, Any]]:
        rng = random.Random(f"{self.symbol}|{day['date']}")  # nosec B311 - deterministic simulation, not security
        o, h, low, c = day["open"], day["high"], day["low"], day["close"]
        v = max(1.0, float(day["volume"]))
        n = SESSION_MINUTES
        f_h, f_l = rng.random(), rng.random()
        if abs(f_h - f_l) < 0.10:
            f_l = (f_h + 0.3) % 1.0
        if f_h < f_l:
            anchors_t = [0.0, f_h, f_l, 1.0]
            anchors_v = [o, h, low, c]
        else:
            anchors_t = [0.0, f_l, f_h, 1.0]
            anchors_v = [o, low, h, c]
        path: list[float] = []
        seg = 0
        for i in range(n):
            t = i / (n - 1)
            while seg < len(anchors_t) - 2 and t > anchors_t[seg + 1]:
                seg += 1
            t0, t1 = anchors_t[seg], anchors_t[seg + 1]
            v0, v1 = anchors_v[seg], anchors_v[seg + 1]
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            path.append(v0 + (v1 - v0) * frac)
        rng_scale = (h - low) * 0.10
        path = [
            p + math.sin(math.pi * i / (n - 1)) * rng.gauss(0, rng_scale)
            for i, p in enumerate(path)
        ]
        pmax, pmin = max(path), min(path)
        if pmax > h or pmin < low:
            span = max(1e-9, pmax - pmin)
            for i in range(1, n - 1):
                path[i] = low + (path[i] - pmin) * (h - low) / span
        start_ms = self._session_start_ms(day["date"])
        bars: list[dict[str, Any]] = []
        prev = o
        base_vol = v / n
        for i in range(n):
            close_i = path[i]
            up = abs(rng.gauss(0, (h - low) * 0.012))
            dn = abs(rng.gauss(0, (h - low) * 0.012))
            hi = max(prev, close_i) + up
            lo = min(prev, close_i) - dn
            vol_i = max(
                1.0, base_vol * _u_shape_volume_weight(i) * rng.lognormvariate(0, 0.5)
            )
            bars.append(
                {
                    "t": start_ms + i * 60_000,
                    "o": round(prev, 2),
                    "h": round(max(hi, prev, close_i), 2),
                    "l": round(min(lo, prev, close_i), 2),
                    "c": round(close_i, 2),
                    "v": int(vol_i),
                    "d": -1,  # filled by the feed (history day index)
                    "m": i,
                }
            )
            prev = close_i
        return bars

    # -- live simulation ------------------------------------------------------

    def start_live_session(self, start_ms: int) -> None:
        gap = max(-0.015, min(0.015, self.rng.gauss(0, self.sigma_daily * 0.3)))
        self.session_open = max(0.05, self.prev_close * (1 + gap))
        self.last_price = self.session_open
        self.session_high = self.session_open
        self.session_low = self.session_open
        self.day_volume = 0.0
        self._session_day = datetime.fromtimestamp(start_ms / 1000.0, tz=IST).strftime(
            "%Y-%m-%d"
        )
        self._cum_tp_vol = 0.0
        self._cum_vol = 0.0
        self._drift = self.rng.gauss(
            0, self.sigma_daily / math.sqrt(SESSION_MINUTES) * 2.4
        )
        self._drift_until = time.monotonic() + 45 + self.rng.random() * 60
        self._day_idx = DEMO_HISTORY_DAYS
        self._minute = 0
        first_t = (start_ms // 60_000) * 60_000
        self._cur_bar = {
            "t": first_t,
            "o": round(self.last_price, 2),
            "h": round(self.last_price, 2),
            "l": round(self.last_price, 2),
            "c": round(self.last_price, 2),
            "v": 0,
            "d": self._day_idx,
            "m": 0,
        }
        self._push_bar(self._cur_bar)

    def _sigma_tick(self) -> float:
        return (
            self.sigma_daily
            / math.sqrt(SESSION_MINUTES)
            * VOL_BOOST
            * math.sqrt(TICK_SECONDS / 60.0)
        )

    def tick(self, now_ms: int, now_mono: float) -> None:
        sig = self._sigma_tick()
        if now_mono > self._drift_until:
            self._drift = self.rng.gauss(
                0, self.sigma_daily / math.sqrt(SESSION_MINUTES) * 2.4
            )
            self._drift_until = now_mono + 45 + self.rng.random() * 60
        vwap = self.vwap if self.vwap > 0 else self.last_price
        mean_rev = -0.10 * (self.last_price - vwap) / max(vwap, 1e-9)
        ret = self._drift / 4.0 + mean_rev / 4.0 + self.rng.gauss(0, sig)
        self.last_price = max(0.05, self.last_price * (1 + ret))

        minute_t = (now_ms // 60_000) * 60_000
        bar = self._cur_bar
        if bar is None:
            return
        if bar["t"] != minute_t:
            self._cur_bar = {
                "t": minute_t,
                "o": round(self.last_price, 2),
                "h": round(self.last_price, 2),
                "l": round(self.last_price, 2),
                "c": round(self.last_price, 2),
                "v": 0,
                "d": self._day_idx,
                "m": self._minute,
            }
            self._minute += 1
            if self._minute >= SESSION_MINUTES:
                # demo day rolls: new session anchored at the current price
                self._minute = 0
                self._day_idx += 1
                self.session_open = self.last_price
                self._cum_tp_vol = 0.0
                self._cum_vol = 0.0
            self._push_bar(self._cur_bar)
            self._on_bar_close(self._cur_bar["o"])
        bar = self._cur_bar
        price = self.last_price
        if price > bar["h"]:
            bar["h"] = round(price, 2)
        if price < bar["l"]:
            bar["l"] = round(price, 2)
        bar["c"] = round(price, 2)
        if price > self.session_high:
            self.session_high = price
        if price < self.session_low:
            self.session_low = price
        # volume: Poisson-ish per tick with bursts
        minute_vol = self.base_min_volume * _u_shape_volume_weight(self._minute)
        tick_vol = max(0.0, random.gauss(minute_vol / 4.0, math.sqrt(minute_vol) / 4.0))
        if self.rng.random() < 0.01:
            tick_vol *= 6.0
        bar["v"] += int(tick_vol)
        self.day_volume += tick_vol
        tp = (price + (bar["h"] + bar["l"]) / 2.0) / 2.0
        self._cum_tp_vol += tp * tick_vol
        self._cum_vol += tick_vol
        if self._cum_vol > 0:
            self.vwap = self._cum_tp_vol / self._cum_vol

    def _push_bar(self, bar: dict[str, Any]) -> None:
        self.bars.append(bar)
        if len(self.bars) > MAX_BARS:
            del self.bars[: len(self.bars) - MAX_BARS]
        self._warm_indicator(bar)

    def _warm_indicator(self, bar: dict[str, Any]) -> None:
        """Incremental EMA9/EMA21, ATR14, RSI14 over the 1-minute close."""
        c = float(bar["c"])
        bar_h = float(bar["h"])
        bar_l = float(bar["l"])
        self.ema_fast_prev = self.ema_fast
        self.ema_slow_prev = self.ema_slow
        self._ema_fast_buf.append(c)
        self._ema_slow_buf.append(c)
        if len(self._ema_fast_buf) == 9:
            self.ema_fast = sum(self._ema_fast_buf) / 9.0
        elif self.ema_fast is not None:
            self.ema_fast += (c - self.ema_fast) * (2.0 / 10.0)
        if len(self._ema_slow_buf) == 21:
            self.ema_slow = sum(self._ema_slow_buf) / 21.0
        elif self.ema_slow is not None:
            self.ema_slow += (c - self.ema_slow) * (2.0 / 22.0)
        if self._prev_close_bar is not None:
            # A true TR needs the previous bar's high/low; approximate with the
            # current bar range plus close-to-close and high/low-to-close moves.
            tr = max(
                bar_h - bar_l,
                abs(c - self._prev_close_bar),
                abs(bar_h - c),
                abs(bar_l - c),
            )
            # ATR14 (Wilder) — own warm-up counter
            if self._atr_count < 14:
                self._atr_sum += tr
                self._atr_count += 1
                if self._atr_count == 14:
                    self.atr = self._atr_sum / 14.0
            else:
                self.atr = (self.atr * 13.0 + tr) / 14.0
            # RSI14 (Wilder) — own warm-up counter
            change = c - self._prev_close_bar
            gain = change if change > 0 else 0.0
            loss = -change if change < 0 else 0.0
            if self._rsi_count < 14:
                self._rsi_gain_sum += gain
                self._rsi_loss_sum += loss
                self._rsi_count += 1
                if self._rsi_count == 14:
                    self._rsi_ave_gain = self._rsi_gain_sum / 14.0
                    self._rsi_ave_loss = self._rsi_loss_sum / 14.0
            else:
                self._rsi_ave_gain = (self._rsi_ave_gain * 13.0 + gain) / 14.0
                self._rsi_ave_loss = (self._rsi_ave_loss * 13.0 + loss) / 14.0
            if self._rsi_ave_loss <= 0:
                self.rsi = 100.0
            else:
                rs = self._rsi_ave_gain / self._rsi_ave_loss
                self.rsi = 100.0 - 100.0 / (1.0 + rs)
        self._prev_close_bar = c

    def _on_bar_close(self, close: float) -> None:  # noqa: ARG002 - hook for the bot
        pass

    # -- real-quote driven updates (LIVE mode) ---------------------------------

    def _recompute_vwap(self) -> None:
        num = 0.0
        den = 0.0
        for bar in self.bars:
            if bar.get("d") != self._day_idx:
                continue
            tp = (bar["h"] + bar["l"] + bar["c"]) / 3.0
            num += tp * bar["v"]
            den += bar["v"]
        self.vwap = num / den if den > 0 else (self.vwap or self.last_price)

    def apply_real_quote(self, q: Any, now_ms: int) -> None:
        """Drive this symbol's state from one real Upstox quote (LIVE mode).

        ``q`` is a :class:`dashboard.live.upstox_source.RealQuote`.  The same
        1-minute bar structures and indicator hooks used by the SIM feed are
        updated, so charts, indicators and the bot behave identically in both
        modes.
        """
        day_key = datetime.fromtimestamp(now_ms / 1000.0, tz=IST).strftime("%Y-%m-%d")
        new_day = day_key != self._session_day
        self._session_day = day_key
        if q.prev_close:
            self.prev_close = q.prev_close
        if q.day_open:
            self.session_open = q.day_open
        if new_day:
            # fresh calendar day: re-seed session extremes from the real quote
            self.session_high = q.day_high or q.last
            self.session_low = q.day_low or q.last
        else:
            if q.day_high:
                self.session_high = max(self.session_high, q.day_high)
            if q.day_low:
                self.session_low = min(self.session_low, q.day_low)
        if q.volume is not None:
            self.day_volume = q.volume
        self.last_price = q.last
        minute_t = (now_ms // 60_000) * 60_000
        bar = self._cur_bar
        # First real quote of the session landing on a bar opened by the SIM
        # walk: if the prices disagree by more than a hair, reset the bar in
        # place to the real price instead of stretching the simulated one.
        if (
            bar is not None
            and bar["t"] == minute_t
            and not bar.get("real")
            and bar["o"]
            and abs(q.last - bar["o"]) / bar["o"] > 0.0025
        ):
            bar["o"] = round(q.last, 2)
            bar["h"] = round(q.last, 2)
            bar["l"] = round(q.last, 2)
            bar["c"] = round(q.last, 2)
            bar["v"] = 0
            bar["vStart"] = q.volume or 0
            bar["real"] = True
            # Re-seed the session extremes so the simulated pre-open anchor
            # does not linger when the real price diverged from it.
            self.session_high = q.last
            self.session_low = q.last
        if bar is None or bar["t"] != minute_t:
            new_bar = {
                "t": minute_t,
                "o": round(q.last, 2),
                "h": round(q.last, 2),
                "l": round(q.last, 2),
                "c": round(q.last, 2),
                "v": 0,
                "d": self._day_idx,
                "m": self._minute,
                "vStart": q.volume or 0,
                "real": True,
            }
            self._cur_bar = new_bar
            self._minute += 1
            if self._minute >= SESSION_MINUTES:
                self._minute = 0
                self._day_idx += 1
                self.session_open = q.last
            self._push_bar(new_bar)
            self._on_bar_close(new_bar["o"])
            bar = new_bar
        if q.last > bar["h"]:
            bar["h"] = round(q.last, 2)
        if q.last < bar["l"]:
            bar["l"] = round(q.last, 2)
        bar["c"] = round(q.last, 2)
        if q.volume is not None:
            bar["v"] = max(0, int(q.volume - bar.get("vStart", 0)))
        self._recompute_vwap()

    def reset_indicators(self) -> None:
        """Full indicator re-warm (used when authoritative bars replace a day)."""
        self.ema_fast = self.ema_slow = None
        self.ema_fast_prev = self.ema_slow_prev = None
        self._ema_fast_buf = []
        self._ema_slow_buf = []
        self._atr_sum = 0.0
        self._atr_count = 0
        self._prev_close_bar = None
        self.atr = 0.0
        self._rsi_ave_gain = 0.0
        self._rsi_ave_loss = 0.0
        self._rsi_gain_sum = 0.0
        self._rsi_loss_sum = 0.0
        self._rsi_count = 0
        self.rsi = 50.0
        for bar in self.bars:
            self._warm_indicator(bar)

    def replace_today_bars(self, bars: list[list[float]]) -> None:
        """Replace today's 1-minute bars with authoritative real bars."""
        if not bars:
            return
        start_idx = len(self.bars)
        for i, existing in enumerate(self.bars):
            if existing.get("d") == self._day_idx:
                start_idx = i
                break
        today = [
            {
                "t": int(row[0]),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
                "d": self._day_idx,
                "m": j,
            }
            for j, row in enumerate(bars)
        ]
        self.bars[start_idx:] = today
        if len(self.bars) > MAX_BARS:
            del self.bars[: len(self.bars) - MAX_BARS]
        self.reset_indicators()
        last = today[-1]
        self._cur_bar = last
        self.session_high = max(self.session_high, last["h"])
        self.session_low = min(self.session_low, last["l"])
        if self.day_volume <= 0:
            self.day_volume = sum(bar["v"] for bar in today)
        self._recompute_vwap()

    def splice_real_history(self, real_bars: list[list[float]]) -> None:
        """Replace the most recent simulated 1-minute days with real bars.

        ``real_bars`` are ascending ``[t_ms, o, h, l, c, v]`` rows from Upstox.
        Simulated bars whose calendar day has real data are dropped; the real
        rows take their place (day indices stay contiguous) and all indicators
        are re-warmed over the corrected history.
        """
        if not real_bars:
            return

        def day_of(t_ms: int) -> str:
            return datetime.fromtimestamp(t_ms / 1000.0, tz=IST).strftime("%Y-%m-%d")

        real_day_set = {day_of(int(row[0])) for row in real_bars}
        if not real_day_set:
            return
        first_t = int(real_bars[0][0])
        last_t = int(real_bars[-1][0])
        # Keep simulated bars strictly before the real range; anything inside
        # or after it is replaced.  After the range, only bars already driven
        # by real quotes are kept — simulated pre-open bars are dropped so a
        # closed market shows the last real session, not fake trades.
        before: list[dict[str, Any]] = [bar for bar in self.bars if bar["t"] < first_t]
        after: list[dict[str, Any]] = [
            bar for bar in self.bars if bar["t"] > last_t and bar.get("real")
        ]
        spliced: list[dict[str, Any]] = [
            {
                "t": int(row[0]),
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
                "d": 0,
                "m": 0,
            }
            for row in real_bars
        ]
        merged = before + spliced + after
        if len(merged) > MAX_BARS:
            del merged[: len(merged) - MAX_BARS]
        # rebuild contiguous day indices in chronological order
        day_order: list[str] = []
        for bar in merged:
            day = day_of(bar["t"])
            if not day_order or day_order[-1] != day:
                day_order.append(day)
        dmap = {day: i for i, day in enumerate(day_order)}
        for bar in merged:
            bar["d"] = dmap[day_of(bar["t"])]
            # m = minute-of-session index (aggregation bucketing depends on it)
            dt = datetime.fromtimestamp(bar["t"] / 1000.0, tz=IST)
            start = dt.replace(hour=9, minute=15, second=0, microsecond=0)
            minutes = int((dt - start).total_seconds() // 60)
            bar["m"] = max(0, min(minutes, SESSION_MINUTES - 1))
        self.bars = merged
        self._day_idx = dmap[day_order[-1]]
        self._session_day = day_order[-1]
        self._minute = 0
        self.reset_indicators()
        current_day_bars = [bar for bar in self.bars if bar["d"] == self._day_idx]
        if current_day_bars:
            self._cur_bar = current_day_bars[-1]
            self.session_open = current_day_bars[0]["o"]
            self.session_high = max(bar["h"] for bar in current_day_bars)
            self.session_low = min(bar["l"] for bar in current_day_bars)
            self.day_volume = sum(bar["v"] for bar in current_day_bars)
        else:
            self._cur_bar = None
        self._recompute_vwap()

    def resume_sim(self) -> None:
        """Resume the simulated random walk from the current price (fallback).

        Keeps all existing history; only re-anchors the live session so there
        is no price jump when the real feed goes away.
        """
        self.session_open = self.last_price
        self._cum_tp_vol = 0.0
        self._cum_vol = 0.0
        self._drift = self.rng.gauss(
            0, self.sigma_daily / math.sqrt(SESSION_MINUTES) * 2.4
        )
        self._drift_until = time.monotonic() + 60.0
        self._minute = 0
        if self._cur_bar is None:
            first_t = (_now_ms() // 60_000) * 60_000
            self._cur_bar = {
                "t": first_t,
                "o": round(self.last_price, 2),
                "h": round(self.last_price, 2),
                "l": round(self.last_price, 2),
                "c": round(self.last_price, 2),
                "v": 0,
                "d": self._day_idx,
                "m": 0,
            }
            self._push_bar(self._cur_bar)
        self._recompute_vwap()

    # -- candles --------------------------------------------------------------

    def candles_1m(self, limit: int) -> list[dict[str, Any]]:
        return self.bars[-max(1, min(int(limit), MAX_BARS)) :]


# ---------------------------------------------------------------------------
# AI demo paper trader
# ---------------------------------------------------------------------------


@dataclass
class BotPosition:
    symbol: str
    quantity: int
    entry: float
    stop: float
    target: float
    opened_at_ms: int
    reason: str


class AIDemoBot:
    """A transparent, demo-only momentum trader.

    Long-only, EMA9/EMA21 cross entries with session-VWAP confirmation and
    ATR14 stop/target management.  Writes virtual fills to the local ledger
    only; it is labelled ``AI DEMO`` everywhere and has no broker access.
    """

    MAX_POSITIONS = 3
    MIN_ENTRY_GAP_SECONDS = 45.0
    REENTER_COOLDOWN_SECONDS = 150.0

    def __init__(self, feed: "LiveFeed") -> None:
        self.feed = feed
        self.enabled = False
        self.risk_pct = 0.10
        self.positions: dict[str, BotPosition] = {}
        self.wins = 0
        self.losses = 0
        self.session_realized = 0.0
        self.last_signal = ""
        self.last_entry_mono = 0.0
        self.cooldown: dict[str, float] = {}
        self.stats_note = "session stats (this server run)"

    # -- persistence ---------------------------------------------------------

    def save_state(self) -> None:
        self.feed.ledger.record_event(
            "ai_demo_state",
            {
                "enabled": self.enabled,
                "risk_pct": self.risk_pct,
                "wins": self.wins,
                "losses": self.losses,
                "session_realized": round(self.session_realized, 2),
                "last_signal": self.last_signal,
                "positions": {
                    s: {
                        "quantity": p.quantity,
                        "entry": p.entry,
                        "stop": p.stop,
                        "target": p.target,
                        "opened_at_ms": p.opened_at_ms,
                        "reason": p.reason,
                    }
                    for s, p in self.positions.items()
                },
            },
        )

    def load_state(self) -> None:
        # events() is newest-first; take the most recent state record
        for event in self.feed.ledger.events(limit=200):
            if event.get("event_type") != "ai_demo_state":
                continue
            detail = event.get("detail") or {}
            self.enabled = bool(detail.get("enabled", False))
            self.risk_pct = max(0.01, min(0.20, float(detail.get("risk_pct", 0.10))))
            self.wins = int(detail.get("wins", 0))
            self.losses = int(detail.get("losses", 0))
            self.session_realized = float(detail.get("session_realized", 0.0))
            self.last_signal = str(detail.get("last_signal", ""))
            raw = detail.get("positions") or {}
            for symbol, pos in raw.items():
                if not isinstance(pos, dict):
                    continue
                self.positions[str(symbol)] = BotPosition(
                    symbol=str(symbol),
                    quantity=int(pos.get("quantity", 0)),
                    entry=float(pos.get("entry", 0.0)),
                    stop=float(pos.get("stop", 0.0)),
                    target=float(pos.get("target", 0.0)),
                    opened_at_ms=int(pos.get("opened_at_ms", 0)),
                    reason=str(pos.get("reason", "")),
                )
            break

    # -- execution -----------------------------------------------------------

    def _charges(self, side: str, value: float) -> float:
        from config.costs import (
            SCENARIO_MARKET_CONDITIONS,
            CostScenario,
            load_charge_table,
        )

        table = load_charge_table()
        conditions = SCENARIO_MARKET_CONDITIONS[CostScenario.BASE]
        side_bps = table.buy_bps if side == "BUY" else table.sell_bps
        return round(
            value * (side_bps + float(conditions["slippage_bps"])) / 10_000.0, 2
        )

    def _enter(self, symbol: str, reason: str) -> bool:
        if not self.enabled or symbol in self.positions:
            return False
        now_mono = time.monotonic()
        if len(self.positions) >= self.MAX_POSITIONS:
            return False
        if now_mono - self.last_entry_mono < self.MIN_ENTRY_GAP_SECONDS:
            return False
        if now_mono - self.cooldown.get(symbol, 0.0) < self.REENTER_COOLDOWN_SECONDS:
            return False
        feed = self.feed
        src = feed.symbols.get(symbol)
        if src is None or not src.ready:
            return False
        price = src.last_price
        if price <= 0:
            return False
        settings = feed.ledger.settings()
        cash = float(settings["cash"])
        equity = feed.equity()
        notional = equity * self.risk_pct
        notional = min(notional, equity * 0.15, cash * 0.97)
        quantity = int(notional / price)
        if quantity <= 0:
            return False
        atr = src.atr if src.atr > 0 else price * 0.004
        stop = round(price - 2.0 * atr, 2)
        target = round(price + 3.0 * atr, 2)
        value = quantity * price
        charges = self._charges("BUY", value)
        result = feed.ledger.execute_virtual_fill(
            strategy_id=BOT_STRATEGY_ID,
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            fill_price=price,
            charges=charges,
            source="live_terminal_sim_feed",
            quote_timestamp=_iso_ms(_now_ms()),
        )
        if result.get("status") != "FILLED":
            self.last_signal = (
                f"AI BUY {quantity} {symbol} rejected: {result.get('reason')}"
            )
            feed.ledger.record_event(
                "ai_demo_rejected",
                {"symbol": symbol, "reason": str(result.get("reason", ""))},
            )
            return False
        self.positions[symbol] = BotPosition(
            symbol=symbol,
            quantity=quantity,
            entry=price,
            stop=stop,
            target=target,
            opened_at_ms=_now_ms(),
            reason=reason,
        )
        self.last_entry_mono = now_mono
        self.last_signal = f"BUY {quantity} {symbol} @ {price:,.2f} ({reason}) · stop {stop:,.2f} · target {target:,.2f}"
        feed.push_fill("BUY", symbol, quantity, price, reason, stop=stop, target=target)
        self.save_state()
        return True

    def _exit(self, symbol: str, reason: str) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        src = self.feed.symbols.get(symbol)
        if src is None:
            return
        price = src.last_price
        if price <= 0:
            return
        quantity = pos.quantity
        value = quantity * price
        charges = self._charges("SELL", value)
        result = self.feed.ledger.execute_virtual_fill(
            strategy_id=BOT_STRATEGY_ID,
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            fill_price=price,
            charges=charges,
            source="live_terminal_sim_feed",
            quote_timestamp=_iso_ms(_now_ms()),
        )
        if result.get("status") != "FILLED":
            self.last_signal = (
                f"AI SELL {quantity} {symbol} rejected: {result.get('reason')}"
            )
            self.feed.ledger.record_event(
                "ai_demo_rejected",
                {"symbol": symbol, "reason": str(result.get("reason", ""))},
            )
            if "insufficient virtual holdings" in str(result.get("reason", "")):
                del self.positions[symbol]
                self.save_state()
            return
        realized = (price - pos.entry) * quantity
        self.session_realized += realized
        if realized >= 0:
            self.wins += 1
        else:
            self.losses += 1
        del self.positions[symbol]
        self.cooldown[symbol] = time.monotonic()
        self.last_signal = (
            f"SELL {quantity} {symbol} @ {price:,.2f} ({reason}) · {realized:+,.0f}"
        )
        self.feed.push_fill(
            "SELL", symbol, quantity, price, reason, realized=round(realized, 2)
        )
        self.save_state()

    # -- strategy ------------------------------------------------------------

    def on_bar_close(self, symbol: str) -> None:
        """Called for every symbol on each 1-minute bar close."""
        src = self.feed.symbols.get(symbol)
        if src is None:
            return
        pos = self.positions.get(symbol)
        if pos is not None:
            # signal-based exit after the stop/target have not triggered
            if (
                src.ema_fast is not None
                and src.ema_slow is not None
                and src.ema_fast < src.ema_slow
                and src.last_price < src.session_open
            ):
                self._exit(symbol, "signal exit")
                return
        if not self.enabled or pos is not None:
            return
        if src.is_index:
            return  # index levels are a benchmark, not a tradable instrument
        if src.ema_fast is None or src.ema_slow is None:
            return
        prev_fast, prev_slow = src.ema_fast_prev, src.ema_slow_prev
        if prev_fast is None or prev_slow is None:
            return
        crossed = prev_fast <= prev_slow and src.ema_fast > src.ema_slow
        above_vwap = src.last_price > (src.vwap if src.vwap > 0 else src.last_price)
        if crossed and above_vwap and src.rsi < 78:
            self._enter(symbol, "momentum cross")

    def check_stops(self) -> None:
        if not self.positions:
            return
        for symbol, pos in list(self.positions.items()):
            src = self.feed.symbols.get(symbol)
            if src is None:
                continue
            price = src.last_price
            if pos.stop > 0 and price <= pos.stop:
                self._exit(symbol, "stop-loss")
            elif pos.target > 0 and price >= pos.target:
                self._exit(symbol, "take-profit")

    def seed_continuation(self) -> None:
        """On enable: take the strongest warm momentum position immediately
        so the operator sees the AI act without waiting for the next cross."""
        if not self.enabled or self.positions:
            return
        best: tuple[float, str] | None = None
        for symbol, src in self.feed.symbols.items():
            if src.is_index or not src.ready:
                continue
            if src.ema_fast is None or src.ema_slow is None:
                continue
            score = (src.ema_fast - src.ema_slow) / max(src.last_price, 1e-9)
            if score > 0 and src.last_price > (src.vwap if src.vwap > 0 else 0):
                if best is None or score > best[0]:
                    best = (score, symbol)
        if best is not None:
            self._enter(best[1], "momentum continuation")

    def status_text(self) -> str:
        if not self.enabled:
            return "Standby — AI paper trader is off"
        open_count = len(self.positions)
        return f"Active — watching {len(self.feed.tradable)} symbols · {open_count}/{self.MAX_POSITIONS} positions open"


# ---------------------------------------------------------------------------
# SSE hub
# ---------------------------------------------------------------------------


class _Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: list[queue.Queue[str]] = []

    def add(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=64)
        with self._lock:
            self._clients.append(q)
        return q

    def remove(self, q: queue.Queue[str]) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def push(self, event: str, payload: dict[str, Any]) -> None:
        message = (
            f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        )
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(message)
            except queue.Full:
                pass  # slow client: drop, it will resync on the next state poll

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._clients)


# ---------------------------------------------------------------------------
# Feed orchestrator
# ---------------------------------------------------------------------------


class LiveFeed:
    def __init__(self, root: Path | str, *, real_source: Any | None = None) -> None:
        """Create the feed.  ``real_source`` (a read-only Upstox source) may be
        injected for tests; otherwise it is built from the environment when an
        access token is configured."""
        self.root = Path(root)
        self.ledger = PaperLedger(self.root / "var" / "paper_trading.sqlite")
        self.hub = _Hub()
        self.symbols: dict[str, SymbolFeed] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started_at_ms = _now_ms()
        self.day_start_equity: float | None = None
        self.upstox_configured = bool(
            os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_SANDBOX_ACCESS_TOKEN")
        )
        self.real: Any | None = None
        self._last_quote_poll = 0.0
        self._consecutive_quote_failures = 0
        self._intraday_rotate_idx = 0
        self._sim_fallback_reason: str | None = None
        eod_dir = self.root / "data" / "eod2" / "daily"
        for symbol, filename, name, is_index in UNIVERSE:
            src = SymbolFeed(symbol, filename, name, is_index, eod_dir)
            if src.ready:
                self.symbols[symbol] = src
        self.tradable = [s for s in self.symbols if not self.symbols[s].is_index]
        self.bot = AIDemoBot(self)
        self._prepare_history()
        self.bot.load_state()
        for symbol, src in self.symbols.items():
            src._on_bar_close = (lambda s: lambda _c: self.bot.on_bar_close(s))(symbol)  # type: ignore[method-assign]
        if real_source is not None:
            self.real = real_source
        elif self.upstox_configured:
            try:
                from .upstox_source import build_upstox_source

                self.real = build_upstox_source(self.root, self.symbols)
            except Exception:  # noqa: BLE001 — real data is an enhancement, never fatal
                logger.exception("upstox_source_init_failed")
                self.real = None

    # -- feed mode -------------------------------------------------------------

    @property
    def mode(self) -> str:
        """LIVE only while real quotes are flowing; otherwise clearly SIM."""
        if self.real is not None and self.real.warmed and self.real.healthy:
            return "LIVE"
        return "SIM"

    def _enter_sim_fallback(self, reason: str) -> None:
        if self.mode == "SIM" and self._sim_fallback_reason:
            return
        self._sim_fallback_reason = reason
        for src in self.symbols.values():
            src.resume_sim()
        self.ledger.record_event("live_feed_sim_fallback", {"reason": reason})
        self.hub.push("feed_mode", {"mode": "SIM", "reason": reason})

    # -- lifecycle ------------------------------------------------------------

    def _prepare_history(self) -> None:
        now_ms = _now_ms()
        for src in self.symbols.values():
            days = src.daily[-DEMO_HISTORY_DAYS:]
            for idx, day in enumerate(days):
                bars = src._reconstruct_day(day)
                for bar in bars:
                    bar["d"] = idx
                    src.bars.append(bar)
                    src._warm_indicator(bar)
            src.start_live_session(now_ms)
        settings = self.ledger.settings()
        self.day_start_equity = self.equity()
        self._initial_capital = float(settings["initial_capital"])
        self.ledger.record_event(
            "ai_demo_feed_started",
            {
                "mode": "SIM",
                "symbols": list(self.symbols),
                "vol_boost": VOL_BOOST,
                "note": "simulated continuous demo feed anchored to verified EOD history",
            },
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        if self.real is not None:
            # warm-up is network-heavy (per-symbol history); run it off-request
            threading.Thread(
                target=self._warm_real, name="live-feed-warmup", daemon=True
            ).start()
        self._thread = threading.Thread(
            target=self._loop, name="live-feed", daemon=True
        )
        self._thread.start()

    def _warm_real(self) -> None:
        try:
            self.real.warm(list(self.symbols))
        except Exception:  # noqa: BLE001
            logger.exception("upstox_warmup_failed")
            self.real.healthy = False
            return
        if not self.real.warmed:
            return
        # splice authoritative 1-minute history over the simulated tail so
        # sub-daily charts show real bars as soon as warm-up completes
        for symbol in list(self.real.real_symbols):
            src = self.symbols.get(symbol)
            bars = self.real.one_m_history(symbol)
            if src is not None and bars:
                src.splice_real_history(bars)
        self.hub.push("feed_mode", {"mode": self.mode, "reason": None})

    def stop(self) -> None:
        self._stop.set()
        if self.real is not None:
            self.real.stop()

    # -- engine loop ------------------------------------------------------------

    def _loop(self) -> None:
        LiveFeed._loop_start = time.monotonic()
        next_tick = time.monotonic()
        while not self._stop.is_set():
            self._cycle()
            next_tick += TICK_SECONDS
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

    def _cycle(self) -> None:
        now_mono = time.monotonic()
        now_ms = _now_ms()
        if self.real is not None and self.mode == "LIVE":
            self._live_cycle(now_mono, now_ms)
            # symbols without a verified Upstox instrument key stay on the
            # (labelled) SIM walk so their prices keep moving in LIVE mode
            for symbol in getattr(self.real, "unmapped_symbols", []):
                src = self.symbols.get(symbol)
                if src is not None:
                    src.tick(now_ms, now_mono)
        else:
            for src in self.symbols.values():
                src.tick(now_ms, now_mono)
        self.hub.push(
            "tick",
            {
                "t": now_ms,
                "p": {
                    s: [
                        round(src.last_price, 2),
                        round(src.session_open, 2),
                        round(src.prev_close, 2),
                        int(src.day_volume),
                        round(src.session_high, 2),
                        round(src.session_low, 2),
                        round(src.vwap, 2) if src.vwap else None,
                    ]
                    for s, src in self.symbols.items()
                },
            },
        )
        if now_mono - self._last_portfolio >= 1.0:
            self._last_portfolio = now_mono
            self.bot.check_stops()
            self.hub.push("portfolio", self._portfolio_payload())
        if now_mono - self._last_persist >= 5.0:
            self._last_persist = now_mono
            self._persist_marks_and_equity()
        if now_mono - self._last_heartbeat >= 15.0:
            self._last_heartbeat = now_mono
            self.hub.push(
                "heartbeat",
                {
                    "t": now_ms,
                    "mode": self.mode,
                    "uptime_s": round(now_mono - self._loop_start, 1),
                    "clients": self.hub.size,
                },
            )

    _loop_start = 0.0
    _last_portfolio = 0.0
    _last_persist = 0.0
    _last_heartbeat = 0.0

    def _live_cycle(self, now_mono: float, now_ms: int) -> None:
        """Quote-driven updates in LIVE mode (no simulated ticks)."""
        if self.real is None:
            return
        if now_mono - self._last_quote_poll < self.real.QUOTE_POLL_SECONDS:
            return
        self._last_quote_poll = now_mono
        quotes = self.real.fetch_quotes(list(self.symbols))
        if quotes:
            self._consecutive_quote_failures = 0
            for symbol, quote in quotes.items():
                src = self.symbols.get(symbol)
                if src is not None:
                    src.apply_real_quote(quote, now_ms)
            self._rotate_intraday_refresh(now_mono, now_ms)
        else:
            self._consecutive_quote_failures += 1
            if self._consecutive_quote_failures >= 3:
                self.real.healthy = False
                self._enter_sim_fallback(
                    "Upstox quote polling failed: " + str(self.real.error or "unknown")
                )

    def _rotate_intraday_refresh(self, now_mono: float, now_ms: int) -> None:
        """Pull authoritative 1-minute bars for one symbol per quote cycle."""
        symbols = list(self.real.real_symbols)
        if not symbols:
            return
        index = self._intraday_rotate_idx % len(symbols)
        self._intraday_rotate_idx += 1
        symbol = symbols[index]
        bars = self.real.refresh_intraday(symbol)
        if bars:
            src = self.symbols.get(symbol)
            if src is not None:
                src.replace_today_bars(bars)

    def _portfolio_payload(self) -> dict[str, Any]:
        positions = []
        market_value = 0.0
        unrealized = 0.0
        for pos in self.ledger.positions():
            symbol = str(pos["symbol"])
            src = self.symbols.get(symbol)
            last = src.last_price if src else None
            qty = int(pos["quantity"])
            avg = float(pos["average_entry_cost"] or 0.0)
            value = last * qty if last else None
            pnl = (last - avg) * qty if last else None
            market_value += value or 0.0
            unrealized += pnl or 0.0
            bot_pos = self.bot.positions.get(symbol)
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": qty,
                    "avg": round(avg, 2),
                    "last": round(last, 2) if last else None,
                    "value": round(value, 2) if value is not None else None,
                    "pnl": round(pnl, 2) if pnl is not None else None,
                    "pnl_pct": round((last / avg - 1.0) * 100.0, 3)
                    if last and avg
                    else None,
                    "strategy": BOT_STRATEGY_ID if bot_pos else "paper",
                    "stop": bot_pos.stop if bot_pos else None,
                    "target": bot_pos.target if bot_pos else None,
                    "entry_at": bot_pos.opened_at_ms if bot_pos else None,
                }
            )
        settings = self.ledger.settings()
        cash = float(settings["cash"])
        equity = cash + market_value
        realized = self.ledger.realized_pnl_total()
        initial = float(settings["initial_capital"])
        day_base = self.day_start_equity if self.day_start_equity else equity
        wins, losses = self.bot.wins, self.bot.losses
        return {
            "t": _now_ms(),
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "market_value": round(market_value, 2),
            "unrealized": round(unrealized, 2),
            "realized": round(realized, 2),
            "today_pnl": round(equity - day_base, 2),
            "total_pnl": round(equity - initial, 2),
            "initial_capital": round(initial, 2),
            "positions": positions,
            "bot": {
                "enabled": self.bot.enabled,
                "risk_pct": self.bot.risk_pct,
                "max_positions": AIDemoBot.MAX_POSITIONS,
                "open": len(self.bot.positions),
                "wins": wins,
                "losses": losses,
                "win_rate": round(wins / (wins + losses), 3)
                if (wins + losses)
                else None,
                "session_realized": round(self.bot.session_realized, 2),
                "last_signal": self.bot.last_signal,
                "status": self.bot.status_text(),
            },
        }

    def _persist_marks_and_equity(self) -> None:
        now_ms = _now_ms()
        held = {str(p["symbol"]) for p in self.ledger.positions()}
        marks = []
        for symbol, src in self.symbols.items():
            if symbol not in held and src.is_index:
                continue
            spread = src.last_price * 0.0002
            marks.append(
                {
                    "symbol": symbol,
                    "instrument_key": f"SIM|{symbol}",
                    "last_price": round(src.last_price, 2),
                    "bid_price": round(src.last_price - spread, 2),
                    "ask_price": round(src.last_price + spread, 2),
                    "volume": src.day_volume,
                    "timestamp": _iso_ms(now_ms),
                    "source": "sim_feed",
                }
            )
        if marks:
            self.ledger.record_marks(marks)
        settings = self.ledger.settings()
        cash = float(settings["cash"])
        market_value = 0.0
        unrealized = 0.0
        for pos in self.ledger.positions():
            src = self.symbols.get(str(pos["symbol"]))
            if src is None:
                continue
            qty = int(pos["quantity"])
            avg = float(pos["average_entry_cost"] or 0.0)
            market_value += qty * src.last_price
            unrealized += qty * (src.last_price - avg)
        equity = cash + market_value
        self.ledger.record_equity(
            {
                "equity": equity,
                "cash": cash,
                "market_value": market_value,
                "realized_pnl": self.ledger.realized_pnl_total(),
                "unrealized_pnl": unrealized,
                "quote_status": "SIM",
            }
        )

    # -- read model ------------------------------------------------------------

    def equity(self) -> float:
        settings = self.ledger.settings()
        cash = float(settings["cash"])
        market_value = 0.0
        for pos in self.ledger.positions():
            src = self.symbols.get(str(pos["symbol"]))
            if src is not None:
                market_value += int(pos["quantity"]) * src.last_price
        return cash + market_value

    def push_fill(
        self,
        side: str,
        symbol: str,
        quantity: int,
        price: float,
        reason: str,
        stop: float | None = None,
        target: float | None = None,
        realized: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "t": _now_ms(),
            "side": side,
            "symbol": symbol,
            "quantity": quantity,
            "price": round(price, 2),
            "reason": reason,
        }
        if stop is not None:
            payload["stop"] = stop
        if target is not None:
            payload["target"] = target
        if realized is not None:
            payload["realized"] = realized
        self.hub.push("fill", payload)

    def candles(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        src = self.symbols.get(symbol)
        if src is None:
            raise KeyError(f"unknown symbol {symbol}")
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {sorted(INTERVALS)}")
        limit = max(10, min(int(limit), 6000))
        if interval in ("1d", "1w"):
            candles = self._daily_candles(src, interval, limit)
        else:
            candles = self._aggregate_1m(src, INTERVALS[interval], limit)
        bot_pos = self.bot.positions.get(symbol)
        return {
            "symbol": symbol,
            "name": src.name,
            "interval": interval,
            "feed": {
                "mode": self.mode,
                "vol_boost": VOL_BOOST,
                "upstox_configured": self.upstox_configured,
            },
            "session": {
                "open": round(src.session_open, 2),
                "prev_close": round(src.prev_close, 2),
                "last": round(src.last_price, 2),
                "vwap": round(src.vwap, 2) if src.vwap else None,
                "session_high": round(src.session_high, 2),
                "session_low": round(src.session_low, 2),
                "volume": int(src.day_volume),
            },
            "indicators": {
                "ema_fast": round(src.ema_fast, 2) if src.ema_fast else None,
                "ema_slow": round(src.ema_slow, 2) if src.ema_slow else None,
                "atr": round(src.atr, 2) if src.atr else None,
                "rsi": round(src.rsi, 2),
            },
            "position": (
                {
                    "quantity": bot_pos.quantity,
                    "entry": bot_pos.entry,
                    "stop": bot_pos.stop,
                    "target": bot_pos.target,
                    "reason": bot_pos.reason,
                    "opened_at_ms": bot_pos.opened_at_ms,
                }
                if bot_pos
                else None
            ),
            "candles": candles,
        }

    def _aggregate_1m(
        self, src: SymbolFeed, bucket: int, limit: int
    ) -> list[list[float]]:
        out: list[list[float]] = []
        cur: list[float] | None = None
        cur_key: int | None = None
        for bar in src.bars:
            key = int(bar["d"]) * 10_000 + int(bar["m"]) // bucket
            if key != cur_key:
                if cur is not None:
                    out.append(cur)
                cur_key = key
                cur = [bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]]
            elif cur is not None:
                if bar["h"] > cur[2]:
                    cur[2] = bar["h"]
                if bar["l"] < cur[3]:
                    cur[3] = bar["l"]
                cur[4] = bar["c"]
                cur[5] += bar["v"]
        if cur is not None:
            out.append(cur)
        return out[-limit:]

    def _daily_candles(
        self, src: SymbolFeed, interval: str, limit: int
    ) -> list[list[float]]:
        now = datetime.now(IST)
        running_t = int(
            now.replace(hour=15, minute=30, second=0, microsecond=0).timestamp() * 1000
        )
        real_daily = (
            self.real.daily_history(src.symbol) if self.real is not None else None
        )
        if real_daily:
            rows = [
                {
                    "date": datetime.fromtimestamp(bar[0] / 1000.0, tz=IST).strftime(
                        "%Y-%m-%d"
                    ),
                    "open": bar[1],
                    "high": bar[2],
                    "low": bar[3],
                    "close": bar[4],
                    "volume": bar[5],
                }
                for bar in real_daily
            ]
        else:
            rows = list(src.daily)
        if interval == "1w":
            weekly: dict[str, list[float]] = {}
            order: list[str] = []
            for row in rows:
                try:
                    day = datetime.fromisoformat(row["date"]).date()
                except ValueError:
                    continue
                week = day.isocalendar()[:2]
                key = f"{week[0]}-{week[1]}"
                if key not in weekly:
                    weekly[key] = [
                        int(datetime(day.year, day.month, day.day).timestamp() * 1000),
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                    ]
                    order.append(key)
                else:
                    bar = weekly[key]
                    bar[2] = max(bar[2], row["high"])
                    bar[3] = min(bar[3], row["low"])
                    bar[4] = row["close"]
                    bar[5] += row["volume"]
            out = [weekly[k] for k in order]
        else:
            out = [
                [
                    int(
                        datetime.fromisoformat(r["date"])
                        .replace(tzinfo=IST)
                        .timestamp()
                        * 1000
                    ),
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                ]
                for r in rows
            ]
        # append the running (simulated) session as the final live candle
        if src.bars and src.session_open > 0:
            running = [
                running_t,
                round(src.session_open, 2),
                round(src.session_high, 2),
                round(src.session_low, 2),
                round(src.last_price, 2),
                int(src.day_volume),
            ]
            if out and out[-1][0] == running_t:
                out[-1] = running
            else:
                out.append(running)
        return out[-limit:]

    def snapshot(self) -> dict[str, Any]:
        portfolio = self._portfolio_payload()
        quotes = []
        for symbol, src in self.symbols.items():
            last = src.last_price
            prev = src.prev_close or last
            quotes.append(
                {
                    "symbol": symbol,
                    "name": src.name,
                    "index": src.is_index,
                    "last": round(last, 2),
                    "open": round(src.session_open, 2),
                    "prev_close": round(prev, 2),
                    "session_high": round(getattr(src, "session_high", last), 2),
                    "session_low": round(getattr(src, "session_low", last), 2),
                    "chg_pct": round((last / prev - 1.0) * 100.0, 2) if prev else 0.0,
                    "vwap": round(src.vwap, 2) if src.vwap else None,
                    "volume": int(src.day_volume),
                    "sigma_daily": round(src.sigma_daily, 5),
                }
            )
        settings = self.ledger.settings()
        now = datetime.now(IST)
        mode = self.mode
        if mode == "LIVE":
            note = "Real Upstox market data (read-only): live quotes + Upstox v3 candle history."
            if self.real is not None:
                extra = sorted(
                    set(getattr(self.real, "unmapped_symbols", []))
                    | set(getattr(self.real, "sim_fallback_symbols", []))
                )
                if extra:
                    note += f" {len(extra)} symbol(s) on SIM fallback: " + ", ".join(
                        extra
                    )
        elif self._sim_fallback_reason:
            note = "SIM fallback (real feed dropped): " + self._sim_fallback_reason
        else:
            note = "Intraday prices are simulated from verified EOD history (data/eod2). No broker connection."
        return {
            "feed": {
                "mode": mode,
                "note": note,
                "vol_boost": VOL_BOOST,
                "upstox_configured": self.upstox_configured,
                "started_at": _iso_ms(self.started_at_ms),
                "upstox": (
                    self.real.status()
                    if self.real is not None
                    else {
                        "configured": False,
                        "detail": "no Upstox access token configured",
                    }
                ),
            },
            "clock": {
                "now_ms": _now_ms(),
                "ist": now.isoformat(),
                "session_open_ms": None,
            },
            "universe": quotes,
            "portfolio": portfolio,
            "orders": self.ledger.order_history(limit=30),
            "equity_history": self.ledger.equity_history(limit=720),
            "paper": {
                "running": bool(settings["running"]),
                "data_mode": str(settings["data_mode"]),
                "auto_paper": bool(settings["auto_paper_enabled"]),
                "initial_capital": float(settings["initial_capital"]),
            },
            "symbols_tradable": [s for s in self.tradable],
            "server_time": datetime.now(UTC).isoformat(),
        }

    def set_bot(self, enabled: bool, risk_pct: float | None = None) -> dict[str, Any]:
        if risk_pct is not None:
            pct = float(risk_pct)
            if not 0.01 <= pct <= 0.20:
                raise ValueError("risk_pct must be in [0.01, 0.20]")
            self.bot.risk_pct = pct
        self.bot.enabled = bool(enabled)
        self.bot.save_state()
        if self.bot.enabled:
            self.bot.seed_continuation()
        self.ledger.record_event(
            "ai_demo_toggled",
            {"enabled": bool(enabled), "risk_pct": self.bot.risk_pct},
        )
        return self._portfolio_payload()["bot"]
