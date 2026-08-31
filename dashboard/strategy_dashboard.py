"""Strategy Dashboard — the validated strategy, its live signal, and the full research leaderboard.

This module answers three questions on one page:

1. **Is any strategy actually working?** — Yes: *MomReM* (Indian Equity Momentum
   + Market-Regime Tilt) passed every validation gate (deflated Sharpe 0.999,
   positive OOS alpha, robust to 3x costs). Its full strategy card is rendered
   here, along with an honest leaderboard of every other family that was tried
   and rejected.

2. **What should I do today?** — ``compute_momrem_signal`` recomputes the
   strategy's exact production logic (20-day cross-sectional momentum, top-20
   equal weight, 100-day SMA regime filter on the equal-weight market proxy)
   against the latest data in ``data/clean/eod2_data`` and returns the current
   regime, the buy basket with quantities for a given capital, the next
   rebalance date, and market breadth.

3. **Why would it ever look like it's "not working"?** — the page surfaces
   data freshness (last bar date vs today) and the exact commands to refresh
   data, so a stale signal is never mistaken for a broken strategy.

Everything is rendered as self-contained HTML + inline SVG (no external CDN,
no JavaScript), so it works from any browser and behind any proxy.
"""

from __future__ import annotations

import glob
import html
import json
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data" / "clean" / "eod2_data"
RESULTS_FILE = ROOT / "research_live" / "results_summary.json"
EQUITY_FILE = ROOT / "research_live" / "deliverables" / "equity.csv"
DRAWDOWN_FILE = ROOT / "research_live" / "deliverables" / "drawdown.csv"
YEARLY_FILE = ROOT / "research_live" / "deliverables" / "yearly.csv"

# MomReM production parameters (from research_live/STRATEGY_REPORT.md)
MOMREM = {
    "lookback": 20,  # 20-day trailing return = momentum score
    "top_n": 20,  # top-20 names, equal weight
    "rebalance": 20,  # every 20 trading days
    "regime_ma": 100,  # equal-weight market proxy vs its 100-day SMA
    "cost_oneway": 0.0015,
}

CACHE_TTL_SECONDS = 300.0
_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, fn, *args, **kwargs):
    """Small TTL cache so repeated page/API hits don't re-read 130+ parquets."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]
    value = fn(*args, **kwargs)
    _cache[key] = (now, value)
    return value


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_panel() -> pd.DataFrame:
    """Return the long-form (date, symbol) OHLCV panel from the clean bundle."""

    def _load() -> pd.DataFrame:
        files = sorted(glob.glob(str(DATA_DIR / "*.parquet")))
        frames = []
        for f in files:
            df = pd.read_parquet(
                f, columns=["date", "symbol", "open", "high", "low", "close", "volume"]
            )
            df = df.dropna(subset=["date", "close"])
            frames.append(df)
        if not frames:
            raise RuntimeError(
                f"no clean price data found under {DATA_DIR} — run `python fetch_data.py`"
            )
        out = pd.concat(frames, ignore_index=True)
        out["date"] = pd.to_datetime(out["date"])
        out = out.drop_duplicates(subset=["date", "symbol"], keep="last")
        out = out.sort_values(["date", "symbol"]).set_index(["date", "symbol"])
        out = out[(out["close"] > 0) & (out["high"] >= out["low"]) & (out["high"] > 0)]
        return out

    return _cached("panel", _load)


def _close_wide() -> pd.DataFrame:
    """Wide symbol x date close matrix for the liquid universe (>=90% coverage since 2016)."""

    def _load() -> pd.DataFrame:
        panel = load_panel()
        wide = panel["close"].unstack("symbol").sort_index()
        sub = wide.loc[wide.index >= pd.Timestamp("2016-01-01")]
        coverage = sub.notna().mean()
        universe = coverage[coverage >= 0.90].index.tolist()
        # require the name to have traded recently (still listed / not delisted)
        last = wide.index[-1]
        recent = wide.loc[last, universe].notna()
        universe = [s for s in universe if recent.get(s, False)]
        return wide[universe]

    return _cached("close_wide", _load)


# ---------------------------------------------------------------------------
# Live signal — the exact MomReM logic, recomputed on latest data
# ---------------------------------------------------------------------------


def compute_momrem_signal(capital: float = 100_000.0) -> dict[str, Any]:
    """Compute today's MomReM signal from the latest data bundle.

    Returns a JSON-serialisable dict with regime, basket, quantities, next
    rebalance, breadth, and a short signal history.
    """

    def _compute() -> dict[str, Any]:
        close = _close_wide()
        if close.empty:
            raise RuntimeError("no data for signal computation")
        dates = close.index
        as_of = dates[-1]
        today = date.today()

        # -- equal-weight market proxy and regime filter ---------------------
        ret = close.pct_change(fill_method=None).fillna(0.0)
        proxy = (1.0 + ret.mean(axis=1)).cumprod()
        ma100 = proxy.rolling(MOMREM["regime_ma"]).mean()
        regime_raw = bool(proxy.iloc[-1] > ma100.iloc[-1])
        # strategy applies a 1-day execution lag: position(t) = regime(t-1)
        regime_lagged = (
            bool(proxy.iloc[-2] > ma100.iloc[-2]) if len(proxy) >= 2 else regime_raw
        )

        # -- momentum score and basket ----------------------------------------
        mom = close.pct_change(MOMREM["lookback"], fill_method=None)
        score_row = mom.iloc[-1].dropna()
        price_row = close.iloc[-1].dropna()
        common = score_row.index.intersection(price_row.index)
        if len(common) < MOMREM["top_n"] + 3:
            raise RuntimeError("too few names with a momentum score to form a basket")
        ranked = score_row[common].sort_values(ascending=False)
        top = ranked.head(MOMREM["top_n"])

        weight = 1.0 / MOMREM["top_n"]
        basket = []
        notional = 0.0
        for sym, score in top.items():
            px = float(price_row[sym])
            amount = capital * weight
            qty = int(amount // px)
            spent = qty * px
            basket.append(
                {
                    "symbol": str(sym),
                    "mom20_pct": round(float(score) * 100.0, 2),
                    "weight_pct": round(weight * 100.0, 2),
                    "amount": round(amount, 2),
                    "qty": max(qty, 0),
                    "last_close": round(px, 2),
                    "spent": round(spent, 2),
                }
            )
            notional += spent
        cash = max(0.0, capital - notional)

        # -- rebalance grid and history ---------------------------------------
        grid = dates[:: MOMREM["rebalance"]]
        last_rebal = grid[grid <= as_of][-1]
        i = dates.get_loc(last_rebal)
        next_i = i + MOMREM["rebalance"]
        if next_i < len(dates):
            next_rebal = dates[next_i]
        else:
            next_rebal = last_rebal + pd.offsets.BDay(MOMREM["rebalance"])

        history = []
        for gd in grid[-10:]:
            g_proxy = proxy.loc[gd]
            g_ma = ma100.loc[gd]
            g_regime = "IN_MARKET" if (g_proxy > g_ma) else "IN_CASH"
            g_mom = mom.loc[gd].dropna()
            top3 = [str(s) for s in g_mom.nlargest(3).index] if len(g_mom) >= 3 else []
            history.append(
                {
                    "date": gd.date().isoformat(),
                    "regime": g_regime,
                    "top3": top3,
                }
            )

        # -- basket return since last rebalance (estimate, net of entry cost) --
        held = top.index.tolist()
        sub = ret.loc[ret.index > last_rebal, held].mean(axis=1).dropna()
        if len(sub):
            est_ret = float((1.0 + sub).prod() - 1.0) - MOMREM["cost_oneway"]
        else:
            est_ret = 0.0

        # -- market breadth ----------------------------------------------------
        sma20 = close.rolling(20).mean()
        above = (close.iloc[-1] > sma20.iloc[-1]).dropna()
        r5 = close.pct_change(5, fill_method=None).iloc[-1].dropna()
        breadth = {
            "above_20d_sma_pct": round(float(above.mean()) * 100.0, 1)
            if len(above)
            else None,
            "advancers_5d": int((r5 > 0).sum()) if len(r5) else 0,
            "decliners_5d": int((r5 < 0).sum()) if len(r5) else 0,
            "universe_size": int(close.shape[1]),
        }

        stale_days = max(0, (today - as_of.date()).days)
        return {
            "as_of": as_of.date().isoformat(),
            "stale_days": stale_days,
            "fresh": stale_days <= 5,
            "capital": float(capital),
            "regime": {
                "state": "IN_MARKET" if regime_raw else "IN_CASH",
                "proxy": round(float(proxy.iloc[-1]), 4),
                "sma100": round(float(ma100.iloc[-1]), 4),
                "proxy_vs_sma_pct": round(
                    (float(proxy.iloc[-1]) / float(ma100.iloc[-1]) - 1.0) * 100.0, 2
                ),
            },
            "position": {
                "state": "IN_MARKET" if regime_lagged else "IN_CASH",
                "note": "1-day execution lag applied (strategy holds what yesterday's regime allowed)",
            },
            "basket": basket,
            "basket_notional": round(notional, 2),
            "cash": round(cash, 2),
            "last_rebalance": last_rebal.date().isoformat(),
            "next_rebalance": next_rebal.date().isoformat(),
            "return_since_rebalance_pct": round(est_ret * 100.0, 2),
            "breadth": breadth,
            "signal_history": history,
            "parameters": {**MOMREM},
            "data": {
                "last_bar": as_of.date().isoformat(),
                "bars_per_symbol_median": int(close.notna().sum(axis=1).median()),
            },
        }

    return _cached(f"signal:{float(capital):.0f}", _compute)


# ---------------------------------------------------------------------------
# Research leaderboard (all families, honest verdicts)
# ---------------------------------------------------------------------------

FAMILY_LABELS = {
    "momrem": "MomReM — Momentum + Regime Filter",
    "dual_ma": "Dual Moving Average (EMA fast / SMA slow)",
    "ma_cross": "Moving Average Cross",
    "donchian": "Donchian Breakout",
    "ts_momentum": "Time-Series Momentum",
    "rsi_rev": "RSI Reversion",
    "bollinger_rev": "Bollinger Reversion",
    "momentum_cs_ls": "CS Momentum Long-Short",
    "reversal_cs_ls": "CS Reversal Long-Short",
    "momentum_cs_ls_vol": "CS Momentum L/S (Vol-Scaled)",
}

# MomReM validation card (source: research_live/deliverables/STRATEGY_REPORT.md)
MOMREM_CARD = {
    "label": "MomReM — Indian Equity Momentum + Market-Regime Tilt",
    "verdict": "VALIDATED",
    "status_color": "#2ea043",
    "full": {
        "cagr": 0.157,
        "sharpe": 0.697,
        "sortino": 0.652,
        "calmar": 0.488,
        "mdd": -0.322,
        "pf": 1.266,
    },
    "is": {"cagr": 0.128, "sharpe": 0.483, "mdd": -0.263},
    "oos": {
        "cagr": 0.193,
        "sharpe": 0.966,
        "sortino": 0.907,
        "calmar": 1.188,
        "mdd": -0.163,
        "pf": 1.324,
    },
    "validation": {
        "deflated_sharpe": 0.999,
        "alpha": 0.03,
        "beta": 0.51,
        "ir": 0.27,
        "cost_sensitivity": {"1x": 0.97, "2x": 0.85, "3x": 0.73},
        "trade_stats": {
            "n": 198,
            "win_rate_pct": 47.98,
            "expectancy_pct_per_month": 1.321,
        },
    },
}


def load_results_summary() -> list[dict[str, Any]]:
    """Leaderboard rows from research_live/results_summary.json + the MomReM card."""

    def _verdict(sharpe: float, alpha: float) -> tuple[str, str]:
        if sharpe < 0:
            return "REJECTED", "Negative OOS Sharpe — edge destroyed by costs"
        if sharpe < 0.5:
            return "REJECTED", "Too weak net of costs"
        if abs(alpha) < 0.01:
            return "BENCHMARK-LIKE", "High Sharpe but ~zero alpha — tracks the market"
        return "PROMISING", "Positive OOS alpha — needs full validation"

    rows: list[dict[str, Any]] = []
    if RESULTS_FILE.is_file():
        with open(RESULTS_FILE) as fh:
            data = json.load(fh)
        for family, res in data.items():
            oos = res.get("oos") or []
            if not oos:
                continue
            best = max(oos, key=lambda x: x["metrics"]["sharpe"])
            m = best["metrics"]
            alpha = float(best.get("alpha", 0.0))
            ir = float(best.get("ir", 0.0))
            verdict, note = _verdict(m["sharpe"], alpha)
            rows.append(
                {
                    "family": family,
                    "label": FAMILY_LABELS.get(family, family),
                    "oos_sharpe": round(m["sharpe"], 3),
                    "oos_cagr": round(m["cagr"], 3),
                    "oos_mdd": round(m["max_dd"], 3),
                    "alpha": round(alpha, 3),
                    "ir": round(ir, 3),
                    "verdict": verdict,
                    "note": note,
                }
            )

    rows.append(
        {
            "family": "momrem",
            "label": MOMREM_CARD["label"],
            "oos_sharpe": MOMREM_CARD["oos"]["sharpe"],
            "oos_cagr": MOMREM_CARD["oos"]["cagr"],
            "oos_mdd": MOMREM_CARD["oos"]["mdd"],
            "alpha": MOMREM_CARD["validation"]["alpha"],
            "ir": MOMREM_CARD["validation"]["ir"],
            "verdict": "VALIDATED",
            "note": "Deflated Sharpe 0.999 · robust to 3× costs · beats benchmark at every liquidity threshold",
        }
    )
    rows.sort(key=lambda r: r["oos_sharpe"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Charts (self-contained SVG — no external JS/CDN)
# ---------------------------------------------------------------------------


def _downsample(series: pd.Series, max_points: int = 600) -> pd.Series:
    if len(series) <= max_points:
        return series
    step = int(np.ceil(len(series) / max_points))
    out = series.iloc[::step]
    return pd.concat([out, series.iloc[[-1]]]).drop_duplicates()


def _svg_equity_curve() -> str:
    """Equity curve of the validated strategy (from deliverables/equity.csv)."""
    if not EQUITY_FILE.is_file():
        return "<p class='muted'>equity.csv not found</p>"
    df = pd.read_csv(EQUITY_FILE, parse_dates=["date"])
    s = _downsample(df.set_index("date")["equity"])
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 260, 64, 16, 16, 30
    lo, hi = float(s.min()), float(s.max())
    span = hi - lo or 1.0
    xs = np.linspace(PAD_L, W - PAD_R, len(s))
    ys = H - PAD_B - (s.values - lo) / span * (H - PAD_T - PAD_B)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    grid = ""
    for k in range(5):
        gy = PAD_T + k * (H - PAD_T - PAD_B) / 4
        val = hi - k * span / 4
        grid += (
            f'<line x1="{PAD_L}" y1="{gy:.1f}" x2="{W - PAD_R}" y2="{gy:.1f}" '
            f'stroke="#21262d" stroke-width="1"/><text x="{PAD_L - 8}" y="{gy + 4:.1f}" '
            f'text-anchor="end" class="axis">{val:.2f}×</text>'
        )
    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Equity curve">'
        f"{grid}"
        f'<polyline points="{pts}" fill="none" stroke="#2ea043" stroke-width="2"/>'
        f'<text x="{PAD_L}" y="{H - 8}" class="axis">2010 → 2026 · net of 15bps one-way costs</text>'
        f"</svg>"
    )


def _svg_drawdown() -> str:
    """Drawdown area chart."""
    if not DRAWDOWN_FILE.is_file():
        return "<p class='muted'>drawdown.csv not found</p>"
    df = pd.read_csv(DRAWDOWN_FILE, parse_dates=["date"])
    s = _downsample(df.set_index("date")["dd"])
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 180, 64, 16, 16, 24
    worst = float(s.min())
    xs = np.linspace(PAD_L, W - PAD_R, len(s))
    ys = H - PAD_B - (s.values - worst) / (-worst or 1.0) * (H - PAD_T - PAD_B)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{PAD_L},{H - PAD_B} " + pts + f" {W - PAD_R},{H - PAD_B}"
    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Drawdown">'
        f'<line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" y2="{H - PAD_B}" stroke="#21262d"/>'
        f'<polygon points="{area}" fill="rgba(248,81,73,0.25)"/>'
        f'<polyline points="{pts}" fill="none" stroke="#f85149" stroke-width="1.5"/>'
        f'<text x="{PAD_L}" y="{H - 8}" class="axis">max drawdown {worst * 100:.1f}% (2019–26 OOS: -16.3%)</text>'
        f"</svg>"
    )


def _svg_yearly_returns() -> str:
    """Grouped bar chart: strategy vs benchmark yearly returns."""
    if not YEARLY_FILE.is_file():
        return "<p class='muted'>yearly.csv not found</p>"
    df = pd.read_csv(YEARLY_FILE)
    W, H, PAD_L, PAD_R, PAD_T, PAD_B = 1000, 240, 44, 16, 16, 26
    vals = pd.concat([df["strat"], df["bench"]])
    lo, hi = float(vals.min()), float(vals.max())
    span = (hi - lo) or 1.0
    zero = H - PAD_B - (0 - lo) / span * (H - PAD_T - PAD_B)
    n = len(df)
    bw = (W - PAD_L - PAD_R) / n
    bars = ""
    for idx, row in df.iterrows():
        x = PAD_L + idx * bw
        for j, col in enumerate(["strat", "bench"]):
            v = float(row[col])
            y = H - PAD_B - (v - lo) / span * (H - PAD_T - PAD_B)
            hgt = max(abs(y - zero), 1.0)
            color = "#2ea043" if v >= 0 else "#f85149"
            opacity = 0.95 if col == "strat" else 0.45
            bars += (
                f'<rect x="{x + 2 + j * bw * 0.42:.1f}" y="{min(y, zero):.1f}" '
                f'width="{bw * 0.36:.1f}" height="{hgt:.1f}" fill="{color}" fill-opacity="{opacity}"/>'
            )
        bars += (
            f'<text x="{x + bw / 2:.1f}" y="{H - 8}" class="axis" text-anchor="middle">'
            f"{int(row['year'])}</text>"
        )
    return (
        f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Yearly returns">'
        f'<line x1="{PAD_L}" y1="{zero:.1f}" x2="{W - PAD_R}" y2="{zero:.1f}" stroke="#8b949e" stroke-dasharray="3 3"/>'
        f"{bars}"
        f'<text x="{W - PAD_R}" y="{PAD_T + 8}" class="axis" text-anchor="end">solid = strategy · faded = equal-weight benchmark</text>'
        f"</svg>"
    )


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--fg:#e6edf3;--muted:#8b949e;
--green:#2ea043;--amber:#d29922;--red:#f85149;--blue:#58a6ff;--purple:#bc8cff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.5}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 80px}
nav{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border)}
nav .brand{font-weight:700;font-size:1.05rem;margin-right:auto}
nav a{color:var(--blue);text-decoration:none;font-size:.9rem;padding:6px 12px;border:1px solid var(--border);border-radius:8px}
nav a:hover{border-color:var(--blue)}
h1{font-size:1.6rem;margin-bottom:4px}
h2{font-size:1.15rem;margin:28px 0 12px}
.sub{color:var(--muted);font-size:.95rem;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:16px}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.75rem;font-weight:700;letter-spacing:.04em}
.badge.green{background:rgba(46,160,67,.15);color:var(--green);border:1px solid rgba(46,160,67,.4)}
.badge.red{background:rgba(248,81,73,.12);color:var(--red);border:1px solid rgba(248,81,73,.4)}
.badge.amber{background:rgba(210,153,34,.12);color:var(--amber);border:1px solid rgba(210,153,34,.4)}
.badge.blue{background:rgba(88,166,255,.12);color:var(--blue);border:1px solid rgba(88,166,255,.4)}
.pill{display:inline-block;padding:4px 14px;border-radius:8px;font-weight:800;font-size:1rem}
.pill.market{background:rgba(46,160,67,.15);color:var(--green);border:1px solid rgba(46,160,67,.5)}
.pill.cash{background:rgba(210,153,34,.15);color:var(--amber);border:1px solid rgba(210,153,34,.5)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:14px}
.kpi{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.kpi .k{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}
.kpi .v{font-size:1.25rem;font-weight:700;margin-top:2px}
.kpi .v.green{color:var(--green)} .kpi .v.red{color:var(--red)} .kpi .v.amber{color:var(--amber)}
table{width:100%;border-collapse:collapse;font-size:.88rem}
th{color:var(--muted);text-align:left;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}
td{padding:8px 10px;border-bottom:1px solid #21262d}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.warn{background:rgba(210,153,34,.08);border:1px solid rgba(210,153,34,.35);border-radius:10px;padding:12px 16px;margin:14px 0;font-size:.9rem}
.ok{background:rgba(46,160,67,.07);border:1px solid rgba(46,160,67,.3);border-radius:10px;padding:12px 16px;margin:14px 0;font-size:.9rem}
code{background:#0d1117;border:1px solid var(--border);border-radius:6px;padding:2px 6px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82rem;color:var(--blue)}
pre{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px 14px;overflow-x:auto;font-family:ui-monospace,Menlo,monospace;font-size:.82rem;color:var(--fg);margin-top:8px}
.chart{width:100%;height:auto;display:block;margin-top:10px}
.axis{fill:var(--muted);font-size:11px;font-family:ui-monospace,Menlo,monospace}
.muted{color:var(--muted);font-size:.85rem}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
ul{padding-left:20px;margin:8px 0}
li{margin:4px 0}
a{color:var(--blue)}
"""


def _page(title: str, body: str, active: str) -> bytes:
    nav_items = [
        ("/strategy", "Strategy Dashboard", "strategy"),
        ("/paper", "Paper Trading", "paper"),
        ("/cockpit", "Research Cockpit", "cockpit"),
        ("/operations", "Operations", "ops"),
    ]
    nav = "".join(
        f'<a href="{href}"{" style=border-color:var(--blue);color:var(--fg)" if a == active else ""}>{label}</a>'
        for href, label, a in nav_items
    )
    doc = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<div class='wrap'><nav><span class='brand'>Quant India</span>{nav}</nav>"
        f"{body}</div></body></html>"
    )
    return doc.encode("utf-8")


def render_strategy_page(capital: float = 100_000.0) -> bytes:
    """Render the full strategy dashboard as a standalone HTML page."""
    try:
        signal = compute_momrem_signal(capital)
        error = None
    except Exception as exc:  # noqa: BLE001 — surface any data problem on the page
        signal = None
        error = str(exc)

    rows = load_results_summary()

    # ---------------- hero ----------------
    hero = """
    <h1>Strategy Dashboard</h1>
    <p class="sub">One validated strategy: <b>MomReM</b> (momentum + market-regime filter).
    Everything else in the research ledger was tried and honestly rejected. This page shows
    the live signal, the buy basket, and the evidence.</p>
    <div class="card">
      <span class="badge green">VALIDATED</span>
      <span class="badge blue">OOS 2019–26 · net of 15 bps costs</span>
      <span class="badge blue">Deflated Sharpe 0.999</span>
      <div class="kpis">
        <div class="kpi"><div class="k">OOS Sharpe</div><div class="v green">0.97</div></div>
        <div class="kpi"><div class="k">OOS CAGR</div><div class="v green">19.3%</div></div>
        <div class="kpi"><div class="k">OOS Max DD</div><div class="v green">-16.3%</div></div>
        <div class="kpi"><div class="k">OOS Calmar</div><div class="v green">1.19</div></div>
        <div class="kpi"><div class="k">OOS Alpha</div><div class="v green">+3.0%</div></div>
        <div class="kpi"><div class="k">Costs 3×</div><div class="v green">0.73 Sharpe</div></div>
      </div>
    </div>
    """

    # ---------------- live signal ----------------
    if error is not None:
        live = f"""
        <h2>Live signal</h2>
        <div class="warn"><b>Signal could not be computed:</b> {html.escape(error)}<br><br>
        Fix data with: <code>python fetch_data.py</code> then <code>python scripts/run_daily.py</code></div>
        """
    else:
        regime = signal["regime"]
        pos = signal["position"]
        reg_pill = (
            '<span class="pill market">IN MARKET</span>'
            if regime["state"] == "IN_MARKET"
            else '<span class="pill cash">IN CASH</span>'
        )
        pos_pill = (
            '<span class="pill market">HOLDING BASKET</span>'
            if pos["state"] == "IN_MARKET"
            else '<span class="pill cash">IN CASH (1-DAY LAG)</span>'
        )
        stale = (
            '<div class="ok">Data is fresh (last bar '
            f"{signal['as_of']}, {signal['stale_days']}d old).</div>"
            if signal["fresh"]
            else '<div class="warn"><b>Data is stale:</b> last bar is '
            f"{signal['as_of']} ({signal['stale_days']} days ago). The signal below is "
            f'"as of" that date — refresh data with <code>python fetch_data.py</code> '
            "before acting on it.</div>"
        )

        if signal["basket"]:
            unaffordable = [b["symbol"] for b in signal["basket"] if b["qty"] == 0]
            rows_html = "".join(
                f"<tr><td>{html.escape(b['symbol'])}</td>"
                f"<td class='num'>{b['mom20_pct']:+.2f}%</td>"
                f"<td class='num'>{b['weight_pct']:.1f}%</td>"
                f"<td class='num'>{b['last_close']:,.2f}</td>"
                + (
                    f"<td class='num'><b>{b['qty']:,}</b></td>"
                    if b["qty"] > 0
                    else f"<td class='num' style='color:var(--muted)' title='1 share costs ₹{b['last_close']:,.0f} — more than the per-name slice'>—</td>"
                )
                + f"<td class='num'>{b['spent']:,.0f}</td></tr>"
                for b in signal["basket"]
            )
            unaffordable_note = (
                f" · <span class='muted'>{', '.join(unaffordable)} cost more than one slice — "
                "raise capital to include them</span>"
                if unaffordable
                else ""
            )
            basket_html = f"""
            <table><thead><tr><th>Symbol</th><th class='num'>20d momentum</th>
            <th class='num'>Weight</th><th class='num'>Last close ₹</th>
            <th class='num'>Qty</th><th class='num'>≈ Invested ₹</th></tr></thead>
            <tbody>{rows_html}</tbody></table>
            <p class='muted' style='margin-top:10px'>Notional ≈ ₹{signal["basket_notional"]:,.0f}
            · cash ≈ ₹{signal["cash"]:,.0f} · rebalanced every 20 trading days
            (next: <b>{signal["next_rebalance"]}</b>) · quantities rounded down to whole shares.{unaffordable_note}</p>
            """
        else:
            basket_html = (
                "<p><b>No basket while the regime filter is off.</b> The strategy holds "
                "cash until the equal-weight market proxy closes back above its 100-day SMA.</p>"
            )

        hist_html = "".join(
            f"<tr><td>{h['date']}</td>"
            f"<td>{'<span class=badge green>IN MARKET</span>' if h['regime'] == 'IN_MARKET' else '<span class=badge amber>IN CASH</span>'}</td>"
            f"<td>{', '.join(html.escape(s) for s in h['top3'])}</td></tr>"
            for h in signal["signal_history"][-8:]
        )

        live = f"""
        <h2>Live signal — as of {signal["as_of"]}</h2>
        {stale}
        <div class="grid2">
          <div class="card">
            <div class="muted">MARKET REGIME (equal-weight proxy vs 100d SMA)</div>
            <div style="margin:10px 0">{reg_pill}</div>
            <table>
              <tr><td>Market proxy</td><td class='num'>{regime["proxy"]:.4f}</td></tr>
              <tr><td>100-day SMA</td><td class='num'>{regime["sma100"]:.4f}</td></tr>
              <tr><td>Proxy vs SMA</td><td class='num'>{regime["proxy_vs_sma_pct"]:+.2f}%</td></tr>
              <tr><td>Strategy position</td><td class='num'>{pos_pill}</td></tr>
              <tr><td>Since last rebalance</td><td class='num'>{signal["return_since_rebalance_pct"]:+.2f}% (est.)</td></tr>
            </table>
            <p class="muted" style="margin-top:8px">{pos["note"]}</p>
          </div>
          <div class="card">
            <div class="muted">MARKET BREADTH (whole universe)</div>
            <table>
              <tr><td>Above 20-day SMA</td><td class='num'>{signal["breadth"]["above_20d_sma_pct"]}%</td></tr>
              <tr><td>Advancers / decliners (5d)</td><td class='num'>{signal["breadth"]["advancers_5d"]} / {signal["breadth"]["decliners_5d"]}</td></tr>
              <tr><td>Universe size</td><td class='num'>{signal["breadth"]["universe_size"]}</td></tr>
              <tr><td>Last rebalance</td><td class='num'>{signal["last_rebalance"]}</td></tr>
              <tr><td>Next rebalance</td><td class='num'>{signal["next_rebalance"]}</td></tr>
            </table>
            <p class="muted" style="margin-top:8px">Breadth confirms/contradicts the regime
            filter — a falling % above the 20-day SMA while the proxy is above its 100-day
            SMA warns of a regime rollover.</p>
          </div>
        </div>
        <div class="card">
          <div class="muted">TODAY'S BASKET — top-20 names by 20-day momentum, equal weight, ₹{signal["capital"]:,.0f} capital</div>
          <div style="margin-top:10px">{basket_html}</div>
        </div>
        <div class="card">
          <div class="muted">RECENT SIGNAL HISTORY (each 20-trading-day rebalance)</div>
          <table style="margin-top:10px"><thead><tr><th>Date</th><th>Regime</th><th>Top-3 momentum names</th></tr></thead>
          <tbody>{hist_html}</tbody></table>
        </div>
        """

    # ---------------- charts ----------------
    charts = f"""
    <h2>Track record (2010 → 2026, net of costs)</h2>
    <div class="card">{_svg_equity_curve()}</div>
    <div class="grid2">
      <div class="card">{_svg_drawdown()}</div>
      <div class="card">{_svg_yearly_returns()}</div>
    </div>
    """

    # ---------------- leaderboard ----------------
    lb_rows = "".join(
        f"<tr><td>{html.escape(r['label'])}</td>"
        f"<td class='num'>{r['oos_sharpe']:.2f}</td>"
        f"<td class='num'>{r['oos_cagr'] * 100:.1f}%</td>"
        f"<td class='num'>{r['oos_mdd'] * 100:.1f}%</td>"
        f"<td class='num'>{r['alpha']:+.3f}</td>"
        f"<td class='num'>{r['ir']:+.2f}</td>"
        f"<td>{'<span class=badge green>VALIDATED</span>' if r['verdict'] == 'VALIDATED' else ('<span class=badge amber>' + html.escape(r['verdict']) + '</span>' if r['verdict'] == 'BENCHMARK-LIKE' else '<span class=badge red>REJECTED</span>')}</td>"
        f"<td class='muted'>{html.escape(r['note'])}</td></tr>"
        for r in rows
    )
    leaderboard = f"""
    <h2>Research leaderboard — every family tested, honest verdicts</h2>
    <div class="card"><table><thead><tr>
      <th>Strategy family</th><th class='num'>OOS Sharpe</th><th class='num'>OOS CAGR</th>
      <th class='num'>OOS MDD</th><th class='num'>Alpha</th><th class='num'>IR</th>
      <th>Verdict</th><th>Why</th>
    </tr></thead><tbody>{lb_rows}</tbody></table>
    <p class="muted" style="margin-top:10px">OOS = out-of-sample 2019–26, net of 15 bps one-way costs.
    Rows from <code>research_live/results_summary.json</code>; the MomReM row from the validated
    strategy card. Trend-following families (dual MA, MA cross, Donchian) show high Sharpe but ~zero
    alpha — they are just long-beta with a trend filter and add nothing over holding the market.</p></div>
    """

    # ---------------- what's next ----------------
    next_steps = """
    <h2>What else can we do with this right now</h2>
    <div class="grid2">
      <div class="card">
        <b>Refresh data (daily, after market close ~15:45 IST)</b>
        <pre>python fetch_data.py          # pull latest NSE daily bars
python scripts/run_daily.py   # one fail-closed paper forward-test day</pre>
        <p class="muted">The dashboard automatically recomputes the signal from whatever
        the freshest bar is. Stale data is the #1 reason the page looks "broken".</p>
      </div>
      <div class="card">
        <b>Get the signal on demand</b>
        <pre>python scripts/daily_signal.py --capital 100000 --save
# optional Telegram alert when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID set
python scripts/daily_signal.py --capital 100000 --telegram</pre>
        <p class="muted">JSON API: <code>GET /api/strategy/signal?capital=100000</code></p>
      </div>
    </div>
    <div class="card">
      <b>Honest conclusions from the research log</b>
      <ul>
        <li><b>MomReM works</b> — deflated Sharpe 0.999, positive OOS alpha, survives 3× costs, beats the equal-weight benchmark at every liquidity threshold. It is a <i>risk-adjusted</i> improvement (Calmar 1.19 vs benchmark), not a raw-return home run.</li>
        <li><b>Short-side edges were all rejected</b> — pairs/stat-arb (OOS Sharpe −1.7) and gap-fade (−3.6) die to borrow costs + STT in a trending market.</li>
        <li><b>The Indian overnight premium is real but untradeable</b> directly (CAGR 47.9% overnight, −16% intraday) — it's already embedded in buy-and-hold. Never go to cash overnight unnecessarily.</li>
        <li><b>Refinements add nothing</b> — low-vol tilt, drawdown overlays, multi-timeframe momentum, volume confirmation all land at the same OOS Sharpe 0.94–0.97. MomReM is a stable local optimum, not a lucky fit.</li>
      </ul>
      <p class="muted" style="margin-top:8px">Next big lever: paper-trade MomReM forward through <code>scripts/run_daily.py</code>, log the
      live-vs-backtest divergence daily, and only then consider real capital on the Capital Ladder screen.</p>
    </div>
    """

    return _page(
        "Strategy Dashboard — Quant India",
        hero + live + charts + leaderboard + next_steps,
        "strategy",
    )


# ---------------------------------------------------------------------------
# JSON API payload
# ---------------------------------------------------------------------------


def build_signal_payload(capital: float = 100_000.0) -> dict[str, Any]:
    """JSON-serialisable signal payload for the API + CLI."""
    signal = compute_momrem_signal(capital)
    payload = {
        "strategy": "momrem",
        "generated_at": datetime.now(UTC).isoformat(),
        "signal": signal,
        "leaderboard": load_results_summary(),
        "card": MOMREM_CARD,
    }
    return payload


def main() -> None:
    """CLI smoke test: print the signal summary."""
    import argparse

    parser = argparse.ArgumentParser(description="Strategy dashboard smoke test")
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument(
        "--html", action="store_true", help="write dashboard.html and exit"
    )
    args = parser.parse_args()

    if args.html:
        out = ROOT / "var" / "dashboard_strategy.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(render_strategy_page(args.capital))
        print(f"wrote {out}")
        return

    sig = compute_momrem_signal(args.capital)
    print(
        f"as_of={sig['as_of']} regime={sig['regime']['state']} "
        f"position={sig['position']['state']} stale={sig['stale_days']}d"
    )
    print(
        f"basket={len(sig['basket'])} names notional=₹{sig['basket_notional']:,.0f} "
        f"cash=₹{sig['cash']:,.0f} next_rebalance={sig['next_rebalance']}"
    )


if __name__ == "__main__":
    main()
