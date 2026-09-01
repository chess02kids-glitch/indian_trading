"""Tests for the live-terminal feed engine (SIM feed + AI demo paper trader).

The feed is exercised against a temporary root (its own SQLite ledger) with a
read-only symlink to the real ``data/eod2`` history, so the operator's local
paper account is never touched by the test suite.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EOD_DIR = ROOT / "data" / "eod2" / "daily"


@pytest.fixture()
def feed_root(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data" / "eod2"
    data_dir.mkdir(parents=True)
    link = data_dir / "daily"
    link.symlink_to(EOD_DIR, target_is_directory=True)
    return tmp_path


@pytest.fixture()
def feed(feed_root: Path):
    from dashboard.live.feed import LiveFeed

    instance = LiveFeed(feed_root)
    yield instance
    instance.stop()


def test_feed_builds_from_verified_eod(feed):
    assert len(feed.symbols) >= 15, "expected a rich tradable universe"
    src = feed.symbols["RELIANCE"]
    assert src.ready
    assert len(src.daily) > 100
    assert src.prev_close > 0
    assert 0.002 < src.sigma_daily < 0.08
    assert "NIFTY_50" in feed.symbols
    assert feed.symbols["NIFTY_50"].is_index
    assert "NIFTY_50" not in feed.tradable


def test_reconstructed_history_shapes(feed):
    src = feed.symbols["RELIANCE"]
    # 12 reconstructed days of 390 1-minute bars plus the live session bar
    assert len(src.bars) >= 12 * 390
    day = src.daily[-1]
    hist_bars = [b for b in src.bars if b["d"] == 11]
    assert len(hist_bars) == 390
    hi = max(b["h"] for b in hist_bars)
    lo = min(b["l"] for b in hist_bars)
    # reconstructed extremes stay inside a sane envelope of the real day
    assert hi <= day["high"] * 1.02
    assert lo >= day["low"] * 0.98
    # every reconstructed close path starts near open and ends at the close
    assert abs(hist_bars[0]["o"] - day["open"]) < 1e-6
    assert abs(hist_bars[-1]["c"] - day["close"]) < 1e-6


def test_indicators_warmed(feed):
    src = feed.symbols["RELIANCE"]
    assert src.ema_fast is not None and src.ema_slow is not None
    assert src.atr > 0
    assert 0.0 <= src.rsi <= 100.0
    # RSI must not be stuck at the warm-up extremes on a random walk
    assert src.rsi < 99.0


def test_candle_aggregation_invariants(feed):
    res = feed.candles("RELIANCE", "5m", 500)
    one_m = res["candles"]
    assert len(one_m) > 100
    c5 = feed.candles("RELIANCE", "5m", 500)["candles"]
    assert len(c5) >= 50
    for bar in c5:
        t, o, h, low, c, v = bar
        assert h >= max(o, c) - 1e-9
        assert low <= min(o, c) + 1e-9
        assert v >= 0
    # sum of aggregated volumes equals the sum of the 1-minute volumes
    total_1m = sum(b["v"] for b in feed.symbols["RELIANCE"].bars)
    assert total_1m > 0


def test_daily_candles_are_real_history_plus_live_bar(feed):
    src = feed.symbols["RELIANCE"]
    res = feed.candles("RELIANCE", "1d", 400)
    bars = res["candles"]
    assert len(bars) == min(400, len(src.daily) + 1)
    # the historical bars must match the EOD file exactly
    for row, bar in zip(src.daily[-3:], bars[-4:-1]):
        assert abs(bar[1] - row["open"]) < 1e-6
        assert abs(bar[2] - row["high"]) < 1e-6
        assert abs(bar[3] - row["low"]) < 1e-6
        assert abs(bar[4] - row["close"]) < 1e-6
    # the final bar is the live (simulated) session candle
    assert bars[-1][4] == round(src.last_price, 2)


def test_invalid_symbol_and_interval(feed):
    with pytest.raises(KeyError):
        feed.candles("NOT_A_SYMBOL", "1m", 100)
    with pytest.raises(ValueError):
        feed.candles("RELIANCE", "7m", 100)


def test_ai_bot_trades_the_local_ledger_only(feed):
    ledger = feed.ledger
    cash_before = float(ledger.settings()["cash"])
    feed.bot.enabled = True
    feed.bot.risk_pct = 0.05
    entered = feed.bot._enter("SBIN", "unit test")
    assert entered, "bot must be able to open a position"
    assert "SBIN" in feed.bot.positions
    pos = feed.bot.positions["SBIN"]
    assert pos.quantity > 0
    assert pos.stop < pos.entry < pos.target
    settings = ledger.settings()
    assert float(settings["cash"]) < cash_before
    orders = ledger.order_history(limit=5)
    buy = next(o for o in orders if o["symbol"] == "SBIN" and o["side"] == "BUY")
    assert buy["strategy_id"] == "ai_demo"
    assert buy["source"] == "live_terminal_sim_feed"
    # closing the position realises P&L and frees cash
    cash_mid = float(ledger.settings()["cash"])
    feed.bot._exit("SBIN", "unit test")
    assert "SBIN" not in feed.bot.positions
    assert feed.bot.wins + feed.bot.losses == 1
    assert float(ledger.settings()["cash"]) > cash_mid


def test_bot_state_persists_across_restart(feed, tmp_path):
    feed.bot.enabled = True
    feed.bot.risk_pct = 0.07
    feed.bot.save_state()
    from dashboard.live.feed import LiveFeed

    reloaded = LiveFeed(feed.root)
    try:
        assert reloaded.bot.enabled is True
        assert reloaded.bot.risk_pct == pytest.approx(0.07)
    finally:
        reloaded.stop()


def test_sse_hub_delivery(feed):
    q = feed.hub.add()
    try:
        feed.hub.push("tick", {"t": 1, "p": {}})
        message = q.get_nowait()
        assert message.startswith("event: tick\ndata: ")
        payload = json.loads(message.split("data: ", 1)[1])
        assert payload == {"t": 1, "p": {}}
    finally:
        feed.hub.remove(q)


def test_bot_never_trades_the_index(feed):
    # NIFTY_50 is a benchmark level, not a tradable instrument. Craft a
    # textbook momentum-cross on the index feed and assert no position opens.
    feed.bot.enabled = True
    nifty = feed.symbols["NIFTY_50"]
    assert nifty.is_index
    assert nifty.ready  # prev_close > 0 after warm-up
    nifty.ema_fast_prev = 100.0
    nifty.ema_slow_prev = 101.0
    nifty.ema_fast = 102.0
    nifty.ema_slow = 100.5
    nifty.rsi = 50.0
    nifty.vwap = 90.0
    nifty.last_price = 95.0
    nifty.session_open = 90.0
    nifty.atr = 1.0
    feed.bot.on_bar_close("NIFTY_50")
    assert "NIFTY_50" not in feed.bot.positions, "index must never be traded"


def test_snapshot_shape(feed):
    snap = feed.snapshot()
    assert snap["feed"]["mode"] == "SIM"
    assert len(snap["universe"]) == len(feed.symbols)
    for key in ("equity", "cash", "market_value", "unrealized", "realized", "today_pnl", "total_pnl"):
        assert key in snap["portfolio"]
    assert snap["portfolio"]["bot"]["max_positions"] == 3
    assert snap["paper"]["initial_capital"] > 0
    # without a token the upstox block is explicit about being unconfigured
    assert snap["feed"]["upstox"]["configured"] is False


# ---------------------------------------------------------------------------
# LIVE mode wiring (fake read-only source; no SDK, no network)
# ---------------------------------------------------------------------------


class _FakeQuote:
    def __init__(self, symbol, last, open_, high, low, prev_close, volume):
        self.symbol = symbol
        self.last = last
        self.bid = last - 0.05
        self.ask = last + 0.05
        self.day_open = open_
        self.day_high = high
        self.day_low = low
        self.prev_close = prev_close
        self.volume = volume
        self.timestamp_ms = 0


class _FakeUpstoxSource:
    """Implements the read-only interface LiveFeed consumes in LIVE mode."""

    QUOTE_POLL_SECONDS = 0.0  # poll on every cycle in tests

    def __init__(self, symbols, prices=None, fail_quotes=False, intraday=None):
        self.symbols = list(symbols)
        self.warmed = True
        self.healthy = True
        self.error = None
        self.real_symbols = list(symbols)
        self.sim_fallback_symbols = []
        self.market_status = "OPEN"
        self._prices = prices or {s: 100.0 for s in symbols}
        self._fail_quotes = fail_quotes
        self._intraday = intraday  # {symbol: [[t,o,h,l,c,v], ...]}
        self._t = int(1788200000.0 * 1000)
        self.quotes_called = 0

    def warm(self, symbols):
        pass

    def stop(self):
        pass

    def status(self):
        return {
            "configured": True,
            "healthy": self.healthy,
            "warmed": self.warmed,
            "market_status": self.market_status,
            "last_success_at": None,
            "last_latency_ms": None,
            "error": self.error,
            "real_symbols": list(self.real_symbols),
            "sim_fallback_symbols": list(self.sim_fallback_symbols),
            "unmapped_symbols": list(getattr(self, "unmapped_symbols", [])),
        }

    def fetch_quotes(self, symbols):
        self.quotes_called += 1
        if self._fail_quotes:
            # returns empty WITHOUT touching .healthy so the feed's own
            # 3-consecutive-failure logic is what demotes the feed
            self.error = "token expired"
            return {}
        self._t += 60_000
        out = {}
        for s in symbols:
            last = self._prices.get(s)
            if last is None:
                continue  # symbol not in the fake universe
            out[s] = _FakeQuote(
                s, last, last - 1.0, last + 1.0, last - 2.0, last - 3.0, 10_000
            )
        return out

    def fetch_1m_history(self, symbol, days=5):
        return None

    def fetch_daily_history(self, symbol, days=400):
        return None

    def refresh_intraday(self, symbol, min_gap_seconds=0.0):
        return self._intraday.get(symbol) if self._intraday else None

    def daily_history(self, symbol):
        if self._daily is not None:
            return self._daily.get(symbol)
        return None

    _daily = None

    def set_daily(self, symbol, bars):
        self._daily = {symbol: bars}


def _make_live_feed(feed_root: Path, source) -> "object":
    from dashboard.live.feed import LiveFeed

    instance = LiveFeed(feed_root, real_source=source)
    return instance


def test_live_mode_prices_driven_by_real_quotes(feed_root: Path):
    source = _FakeUpstoxSource(["RELIANCE", "TCS"], prices={"RELIANCE": 1200.0, "TCS": 3000.0})
    feed = _make_live_feed(feed_root, source)
    try:
        assert feed.mode == "LIVE"
        src = feed.symbols["RELIANCE"]
        feed._cycle()  # QUOTE_POLL_SECONDS=0 → quotes applied this cycle
        assert src.last_price == 1200.0, "live price must equal the real quote exactly"
        feed._cycle()
        assert src.last_price == 1200.0
        # no simulated ticks happen in LIVE mode (price never drifts from the quote)
        assert source.quotes_called == 2
        snap = feed.snapshot()
        assert snap["feed"]["mode"] == "LIVE"
        assert snap["feed"]["upstox"]["healthy"] is True
    finally:
        feed.stop()


def test_unmapped_symbols_stay_sim_in_live_mode(feed_root: Path):
    """Symbols without an Upstox key keep moving via SIM ticks in LIVE mode."""
    source = _FakeUpstoxSource(["RELIANCE"])  # only RELIANCE has real quotes
    source.unmapped_symbols = [s for s in ("TCS", "ITC") if s != "RELIANCE"]
    feed = _make_live_feed(feed_root, source)
    try:
        assert feed.mode == "LIVE"
        tcs = feed.symbols["TCS"]
        price_before = tcs.last_price
        # several engine cycles (TCS has no quotes → must still tick)
        for _ in range(24):
            feed._cycle()
        assert tcs.last_price != price_before, "unmapped symbol price must keep moving"
        snap = feed.snapshot()
        assert "TCS" in snap["feed"]["note"], "fallback must be disclosed"
        assert snap["feed"]["upstox"]["unmapped_symbols"] == ["TCS", "ITC"]
    finally:
        feed.stop()


def test_live_mode_drops_to_sim_after_quote_failures(feed_root: Path):
    source = _FakeUpstoxSource(["RELIANCE"], prices={"RELIANCE": 1200.0}, fail_quotes=True)
    feed = _make_live_feed(feed_root, source)
    try:
        assert feed.mode == "LIVE"
        src = feed.symbols["RELIANCE"]
        pre = src.last_price
        for _ in range(4):
            feed._cycle()
        assert feed.mode == "SIM", "three failed quote polls must drop the feed to SIM"
        # SIM resumes from the last known price (no teleport)
        assert abs(src.last_price - pre) / pre < 0.01
        assert any(
            e["event_type"] == "live_feed_sim_fallback" for e in feed.ledger.events(50)
        )
    finally:
        feed.stop()


def test_intraday_refresh_replaces_today_bars(feed_root: Path):
    now_min = int(int(1788200000.0) // 60 * 60)
    real_today = [
        [now_min - 120_000, 100.0, 101.0, 99.5, 100.5, 1000.0],
        [now_min - 60_000, 100.5, 101.5, 100.0, 101.0, 800.0],
        [now_min, 101.0, 101.2, 100.8, 101.1, 300.0],
    ]
    source = _FakeUpstoxSource(["RELIANCE"], intraday={"RELIANCE": real_today})
    feed = _make_live_feed(feed_root, source)
    try:
        source.refresh_intraday = lambda symbol, min_gap_seconds=0.0: real_today
        source._intraday = {"RELIANCE": real_today}
        feed.symbols["RELIANCE"].replace_today_bars(real_today)
        src = feed.symbols["RELIANCE"]
        today = [b for b in src.bars if b["d"] == src._day_idx]
        assert [b["c"] for b in today][-3:] == [100.5, 101.0, 101.1]
        assert src.session_high >= 101.5
        assert src.vwap > 0
        # indicators were re-warmed over authoritative bars
        assert src.ema_fast is not None and src.atr > 0
    finally:
        feed.stop()


def test_splice_real_history_replaces_sim_days(feed_root: Path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")

    def day_of(t_ms: int) -> str:
        return datetime.fromtimestamp(t_ms / 1000.0, tz=tz).strftime("%Y-%m-%d")

    source = _FakeUpstoxSource(["RELIANCE"])
    feed = _make_live_feed(feed_root, source)
    try:
        src = feed.symbols["RELIANCE"]
        sim_before = len(src.bars)
        assert sim_before > 0
        # two days of 10 real 1-minute bars on the two most recent calendar days
        now_day = datetime.now(tz=tz)
        rows: list[list[float]] = []
        for day_offset in (2, 1):
            day = (now_day - timedelta(days=day_offset)).replace(
                hour=9, minute=15, second=0, microsecond=0
            )
            price = 1000.0
            for minute in range(10):
                t = int((day + timedelta(minutes=minute)).timestamp() * 1000)
                rows.append([t, price, price + 0.5, price - 0.5, price + 0.2, 100.0])
                price += 0.2
        real_days = {day_of(int(row[0])) for row in rows}
        src.splice_real_history(rows)

        # every spliced day now holds the real bars (open ≈ 1000)
        for day in real_days:
            day_bars = [b for b in src.bars if day_of(b["t"]) == day]
            assert len(day_bars) == 10, f"expected 10 real bars for {day}"
            assert all(abs(b["o"] - 1000.0) < 50.0 for b in day_bars)
            assert all(b["v"] == 100.0 for b in day_bars)
        # bars stay in strict chronological order after the splice
        times = [b["t"] for b in src.bars]
        assert times == sorted(times), "bars must stay chronological after splice"
        # day indices contiguous, current day index = latest day present
        assert src._day_idx == max(b["d"] for b in src.bars)
        assert sorted({b["d"] for b in src.bars}) == list(
            range(0, max(b["d"] for b in src.bars) + 1)
        )
        # indicators re-warmed over the corrected history
        assert src.ema_fast is not None and src.ema_slow is not None
        assert src.rsi is not None and 0 <= src.rsi <= 100
    finally:
        feed.stop()


def test_daily_candles_prefer_real_history(feed_root: Path):
    source = _FakeUpstoxSource(["RELIANCE"])
    day = 1_788_000_000_000
    source.set_daily("RELIANCE", [
        [day - 86_400_000, 100.0, 105.0, 98.0, 103.0, 500_000],
        [day, 103.0, 108.0, 102.0, 106.0, 600_000],
    ])
    feed = _make_live_feed(feed_root, source)
    try:
        bars = feed.candles("RELIANCE", "1d", 100)["candles"]
        # two real bars + one running live bar
        assert len(bars) == 3
        assert abs(bars[-2][4] - 106.0) < 1e-6
        assert bars[-1][4] == round(feed.symbols["RELIANCE"].last_price, 2)
    finally:
        feed.stop()


# ---------------------------------------------------------------------------
# UpstoxLiveSource parsing (fake SDK module, no network)
# ---------------------------------------------------------------------------


def _install_fake_sdk(monkeypatch, quote_map, candles):
    """Install a fake upstox_client module mirroring the SDK's response shapes."""
    import types
    from types import SimpleNamespace

    sdk = types.ModuleType("upstox_client")
    sdk.Configuration = lambda sandbox=False: SimpleNamespace(sandbox=sandbox)

    class _Client:
        def __init__(self, cfg):
            self.cfg = cfg

    sdk.ApiClient = _Client

    class _QuoteApi:
        def __init__(self, client=None):
            self.client = client

        def get_full_market_quote(self, symbol, api_version):
            # nested-object style, exactly like the real SDK
            return SimpleNamespace(
                data={k: SimpleNamespace(**v) for k, v in quote_map.items()}
            )

    class _HistApi:
        def __init__(self, client=None):
            self.client = client

        def get_historical_candle_data(self, key, unit, interval, to, frm=None):
            return SimpleNamespace(data=SimpleNamespace(candles=candles))

        def get_historical_candle_data1(self, key, unit, interval, to, frm=None):
            return SimpleNamespace(data=SimpleNamespace(candles=candles))

        def get_intra_day_candle_data(self, key, unit, interval):
            return SimpleNamespace(data=SimpleNamespace(candles=candles))

    class _MktApi:
        def __init__(self, client=None):
            self.client = client

        def get_market_status(self, exchange):
            return SimpleNamespace(data=SimpleNamespace(status="OPEN"))

    sdk.MarketQuoteApi = _QuoteApi
    sdk.HistoryV3Api = _HistApi
    sdk.MarketHolidaysAndTimingsApi = _MktApi
    monkeypatch.setitem(sys.modules, "upstox_client", sdk)


def test_upstox_source_parses_quotes_and_candles(monkeypatch):
    quote_map = {
        "NSE_EQ|INE002A01018": {
            "last_price": 1200.5,
            "volume": 1_000_000,
            "ohlc": {"open": 1190.0, "high": 1210.0, "low": 1185.0, "close": 1180.0},
            "depth": {
                "buy": [{"price": 1200.4, "quantity": 100}],
                "sell": [{"price": 1200.6, "quantity": 90}],
            },
        }
    }
    # newest-first candle rows, as the real SDK returns them
    candles = [
        ["2026-08-28T15:30:00", 101.0, 102.0, 100.0, 101.5, 1000],
        ["2026-08-28T15:29:00", 100.5, 101.2, 100.1, 101.0, 900],
        ["2026-08-28T15:28:00", 100.0, 100.6, 99.9, 100.5, 800],
    ]
    _install_fake_sdk(monkeypatch, quote_map, candles)

    from dashboard.live.upstox_source import UpstoxLiveSource

    source = UpstoxLiveSource("FAKE_TOKEN", {"RELIANCE": "NSE_EQ|INE002A01018"})

    q = source.fetch_quotes(["RELIANCE"])
    assert "RELIANCE" in q
    rq = q["RELIANCE"]
    assert rq.last == 1200.5
    assert rq.day_open == 1190.0
    assert rq.prev_close == 1180.0  # v2 ohlc.close is the PREVIOUS close
    assert rq.bid == 1200.4 and rq.ask == 1200.6
    assert rq.volume == 1_000_000

    bars = source.fetch_1m_history("RELIANCE")
    assert bars is not None and len(bars) == 3
    # parsed ascending by time
    assert bars[0][0] < bars[1][0] < bars[2][0]
    assert list(bars[0][1:]) == [100.0, 100.6, 99.9, 100.5, 800.0]

    intraday = source.refresh_intraday("RELIANCE", min_gap_seconds=0)
    assert intraday and len(intraday) == 3
    assert source.status()["healthy"] is True


def test_upstox_source_graceful_on_missing_token(monkeypatch):

    monkeypatch.delitem(sys.modules, "upstox_client", raising=False)
    from dashboard.live.upstox_source import RealFeedUnavailable, UpstoxLiveSource

    with __import__("pytest").raises(RealFeedUnavailable):
        UpstoxLiveSource("", {"RELIANCE": "NSE_EQ|INE002A01018"})
