"""Analytics that the unified dashboard renders: regime, divergence, cost
sensitivity, strategy correlation, and position sizing.

Every function here is deterministic given the data bundle, cached with a TTL,
and honest about what it was computed on (symbol count + date range are returned
alongside the numbers, so a chart can never be mistaken for the published
research card unless it *is* the published research card).
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from datahub.panel import strategy_frame, wide

logger = logging.getLogger(__name__)

TRADING_DAYS = 252
RISK_FREE = 0.06  # matches research_live.metrics default

_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 600.0


def _cached(key: str, fn, *args, **kwargs):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL:
        return hit[1]
    value = fn(*args, **kwargs)
    _cache[key] = (now, value)
    return value


def _frame_meta(close: pd.DataFrame) -> dict[str, Any]:
    return {
        "symbols": int(close.shape[1]),
        "dates": int(close.shape[0]),
        "start": str(close.index[0].date()),
        "end": str(close.index[-1].date()),
    }


# ---------------------------------------------------------------------------
# MomReM target weights (the strategy's own definition, vectorised)
# ---------------------------------------------------------------------------

MOMREM_PARAMS = {
    "lookback": 20,
    "top_n": 20,
    "rebalance": 20,
    "regime_ma": 100,
    "cost_oneway": 0.0015,
}


def market_proxy(close: pd.DataFrame) -> pd.Series:
    """Equal-weight market proxy used by the regime filter."""
    ret = close.pct_change(fill_method=None).fillna(0.0)
    return (1.0 + ret.mean(axis=1)).cumprod()


def momrem_targets(
    close: pd.DataFrame,
    *,
    lookback: int = MOMREM_PARAMS["lookback"],
    top_n: int = MOMREM_PARAMS["top_n"],
    rebalance: int = MOMREM_PARAMS["rebalance"],
    regime_ma: int = MOMREM_PARAMS["regime_ma"],
    apply_regime: bool = True,
) -> pd.DataFrame:
    """Target-weight matrix for MomReM (decision at close t, lag applied later).

    Vectorised equivalent of the loop in ``research_live.mom_overlay.mom_tgt``:
    every ``rebalance``-th day, equal-weight the ``top_n`` names by trailing
    ``lookback``-day return, forward-fill between rebalances, then gate the whole
    book by the equal-weight proxy vs its ``regime_ma`` SMA.
    """
    mom = close.pct_change(lookback, fill_method=None)
    values = np.zeros(close.shape, dtype=float)
    dates = close.index
    mom_values = mom.to_numpy()
    row_idx = list(range(0, len(dates), rebalance))
    current = np.zeros(close.shape[1], dtype=float)
    weight = 1.0 / top_n
    for i in range(len(dates)):
        if i in row_idx:
            scores = mom_values[i]
            valid = np.isfinite(scores)
            if valid.sum() >= top_n + 3:
                order = np.argsort(np.where(valid, scores, -np.inf))[::-1][:top_n]
                current = np.zeros(close.shape[1], dtype=float)
                current[order] = weight
        values[i] = current
    targets = pd.DataFrame(values, index=dates, columns=close.columns)

    if apply_regime:
        proxy = market_proxy(close)
        ma = proxy.rolling(regime_ma).mean()
        gate = (proxy > ma).astype(float)
        gate[ma.isna()] = 0.0  # no regime information yet -> stay flat
        targets = targets.mul(gate, axis=0)
    return targets


def simulate_weights(
    close: pd.DataFrame,
    targets: pd.DataFrame,
    *,
    cost_oneway: float = MOMREM_PARAMS["cost_oneway"],
    exec_lag: int = 1,
) -> dict[str, Any]:
    """Simulate a target-weight matrix with costs; return returns + equity."""
    lagged = targets.shift(exec_lag).fillna(0.0)
    ret = close.pct_change(fill_method=None).fillna(0.0)
    r = ret.reindex_like(lagged).to_numpy()
    t = np.clip(lagged.to_numpy(), 0.0, 1.0)
    gross = t.sum(axis=1, keepdims=True)
    t = t * np.minimum(1.0 / np.maximum(gross, 1e-12), 1.0)
    port = (t * r).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        w_end = t * (1.0 + r)
    w_end = w_end / np.maximum(1.0 + port[:, None], 1e-12)
    t_next = np.vstack([t[1:], t[-1:]])
    turnover = np.abs(t_next - w_end).sum(axis=1)
    net = (1.0 + port) - turnover * cost_oneway
    returns = pd.Series(net - 1.0, index=lagged.index, name="ret")
    equity = pd.Series(np.cumprod(net), index=lagged.index, name="equity")
    return {
        "returns": returns,
        "equity": equity,
        "turnover": pd.Series(turnover, index=lagged.index, name="turnover"),
    }


def performance(returns: pd.Series, equity: pd.Series) -> dict[str, float]:
    """Sharpe / CAGR / MDD / Calmar / vol, same conventions as research_live."""
    rets = returns.dropna()
    n = len(rets)
    if n < 2 or float(equity.iloc[0]) <= 0:
        return {
            "cagr": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "max_dd": 0.0,
            "vol": 0.0,
            "win_rate": 0.0,
            "n_days": int(n),
        }
    years = n / TRADING_DAYS
    total = float(equity.iloc[-1]) / float(equity.iloc[0]) - 1.0
    cagr = (float(equity.iloc[-1]) / float(equity.iloc[0])) ** (1.0 / years) - 1.0
    vol = float(rets.std(ddof=1) * math.sqrt(TRADING_DAYS))
    sharpe = (cagr - RISK_FREE) / vol if vol > 0 else 0.0
    dd = equity / equity.cummax() - 1.0
    max_dd = float(dd.min())
    downside = rets[rets < 0]
    downside_dev = (
        float(downside.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(downside) > 1 else 0.0
    )
    sortino = (cagr - RISK_FREE) / downside_dev if downside_dev > 0 else 0.0
    return {
        "cagr": float(cagr),
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "calmar": float(cagr / abs(max_dd)) if max_dd < 0 else 0.0,
        "max_dd": max_dd,
        "vol": vol,
        "win_rate": float((rets > 0).mean()),
        "total_return": float(total),
        "n_days": int(n),
        "years": float(years),
    }


# ---------------------------------------------------------------------------
# 1. Regime tagging
# ---------------------------------------------------------------------------

REGIME_COLORS = {
    "TREND_UP": "#2ea043",
    "TREND_DOWN": "#f85149",
    "CHOPPY": "#d29922",
    "HIGH_VOL": "#bc8cff",
    "IN_CASH": "#6e7681",
}


def regime_series(close: pd.DataFrame, *, regime_ma: int = 100) -> pd.DataFrame:
    """Per-day regime tags for the whole history.

    Three independent lenses, combined into one label:

    * ``trend``      Kaufman efficiency ratio over 20 days — is the market
                     actually going somewhere, or just oscillating?
    * ``vol``        realised 20-day vol of the proxy, bucketed by its own
                     historical percentile (LOW / NORMAL / HIGH).
    * ``filter``     the strategy's own gate: proxy above its 100-day SMA.
    """
    proxy = market_proxy(close)
    proxy_ret = proxy.pct_change(fill_method=None).fillna(0.0)
    ma = proxy.rolling(regime_ma).mean()
    above = proxy > ma

    # Kaufman efficiency ratio: |net move| / sum of |daily moves|
    window = 20
    net = proxy.diff(window).abs()
    path = proxy.diff().abs().rolling(window).sum()
    efficiency = (net / path.replace(0.0, np.nan)).fillna(0.0)

    vol20 = proxy_ret.rolling(20).std() * math.sqrt(TRADING_DAYS)
    vol_rank = vol20.rolling(756, min_periods=60).rank(pct=True)

    trend = pd.Series("CHOPPY", index=proxy.index)
    trend[efficiency >= 0.30] = "TREND"
    direction = np.sign(proxy.diff(window).fillna(0.0))
    trend_label = np.where(
        trend.eq("TREND"),
        np.where(direction > 0, "TREND_UP", "TREND_DOWN"),
        "CHOPPY",
    )
    vol_label = np.where(
        vol_rank.fillna(0.5) >= 0.80,
        "HIGH_VOL",
        np.where(vol_rank.fillna(0.5) <= 0.20, "LOW_VOL", "NORMAL_VOL"),
    )
    out = pd.DataFrame(
        {
            "proxy": proxy,
            "ma": ma,
            "above_ma": above,
            "efficiency": efficiency,
            "vol20": vol20,
            "trend": trend_label,
            "vol_regime": vol_label,
            "filter": np.where(above, "IN_MARKET", "IN_CASH"),
        },
        index=proxy.index,
    )
    out["label"] = np.where(
        ~above,
        "IN_CASH",
        out["trend"].astype(str)
        + np.where(out["vol_regime"].eq("HIGH_VOL"), " · HIGH VOL", ""),
    )
    return out


def regime_summary(close: pd.DataFrame, *, regime_ma: int = 100) -> dict[str, Any]:
    """Current regime + a per-segment breakdown for colouring the equity curve."""

    def _compute() -> dict[str, Any]:
        series = regime_series(close, regime_ma=regime_ma)
        latest = series.iloc[-1]
        # contiguous segments of the strategy filter + trend label
        key = series["label"].astype(str)
        change = key.ne(key.shift())
        group = change.cumsum()
        segments = []
        for _, chunk in series.groupby(group):
            # Label and colour must come from the chunk itself.  Indexing the
            # full `key` series positionally here made every segment inherit the
            # first row of the whole history, so the equity curve was coloured
            # as one long IN_CASH band while `current.label` read TREND_UP.
            chunk_label = str(chunk["label"].iloc[0])
            segments.append(
                {
                    "start": str(chunk.index[0].date()),
                    "end": str(chunk.index[-1].date()),
                    "label": chunk_label,
                    "days": int(len(chunk)),
                    "color": REGIME_COLORS.get(chunk_label.split(" · ")[0], "#30363d"),
                }
            )
        counts = key.value_counts().to_dict()
        total = int(len(key))
        return {
            "current": {
                "as_of": str(series.index[-1].date()),
                "label": str(latest["label"]),
                "filter": str(latest["filter"]),
                "trend": str(latest["trend"]),
                "vol_regime": str(latest["vol_regime"]),
                "proxy": round(float(latest["proxy"]), 4),
                "sma": round(float(latest["ma"]), 4),
                "proxy_vs_sma_pct": round(
                    (float(latest["proxy"]) / float(latest["ma"]) - 1.0) * 100.0, 2
                ),
                "efficiency": round(float(latest["efficiency"]), 3),
                "vol20_annualised_pct": round(float(latest["vol20"]) * 100.0, 2),
            },
            "shares_pct": {
                str(k): round(float(v) / total * 100.0, 1) for k, v in counts.items()
            },
            "segments": segments[-60:],
            "colors": REGIME_COLORS,
            "meta": _frame_meta(close),
        }

    return _cached(f"regime:{close.shape[1]}:{regime_ma}", _compute)


def regime_tagged_equity(
    equity_dates: Sequence[str],
    close: pd.DataFrame,
    *,
    regime_ma: int = 100,
) -> list[dict[str, Any]]:
    """Map regime labels onto an arbitrary equity curve's dates (nearest prior)."""
    series = regime_series(close, regime_ma=regime_ma)
    labels = series["label"].astype(str)
    index = labels.index
    stamps = pd.to_datetime(list(equity_dates))
    positions = index.searchsorted(stamps, side="right") - 1
    out = []
    for stamp, pos in zip(stamps, positions):
        if pos < 0:
            out.append(
                {"date": str(stamp.date()), "label": "NO DATA", "color": "#30363d"}
            )
            continue
        label = str(labels.iloc[pos])
        out.append(
            {
                "date": str(stamp.date()),
                "label": label,
                "color": REGIME_COLORS.get(label.split(" · ")[0], "#30363d"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 2. Cost / slippage sensitivity
# ---------------------------------------------------------------------------


def cost_sensitivity(
    close: pd.DataFrame | None = None,
    *,
    costs_bps: Sequence[float] = (2.5, 5, 7.5, 10, 15, 20, 25, 30, 40),
    oos_start: str | None = "2019-01-01",
) -> dict[str, Any]:
    """Re-run the real MomReM backtest across a grid of one-way costs.

    ``costs_bps`` is the *one-way* cost in basis points (commission + STT +
    slippage).  15 bps is the research default; 30 bps is the stress case.
    """

    def _compute() -> dict[str, Any]:
        frame = close if close is not None else strategy_frame()[0]
        targets = momrem_targets(frame)
        rows = []
        for bps in costs_bps:
            sim = simulate_weights(frame, targets, cost_oneway=bps / 10_000.0)
            window = (
                sim["returns"].index >= pd.Timestamp(oos_start)
                if oos_start
                else slice(None)
            )
            m = performance(sim["returns"][window], sim["equity"][window])
            full = performance(sim["returns"], sim["equity"])
            rows.append(
                {
                    "cost_bps_one_way": float(bps),
                    "round_trip_bps": float(bps) * 2,
                    "sharpe": round(m["sharpe"], 3),
                    "cagr": round(m["cagr"], 4),
                    "max_dd": round(m["max_dd"], 4),
                    "calmar": round(m["calmar"], 3),
                    "sharpe_full": round(full["sharpe"], 3),
                    "cagr_full": round(full["cagr"], 4),
                    "annual_turnover": round(
                        float(sim["turnover"].mean()) * TRADING_DAYS, 2
                    ),
                    "cost_drag_annual_pct": round(
                        float(sim["turnover"].mean()) * TRADING_DAYS * bps / 10_000.0 * 100.0,
                        2,
                    ),
                }
            )
        baseline = next((r for r in rows if abs(r["cost_bps_one_way"] - 15.0) < 1e-9), rows[0])
        first_positive = next((r for r in rows if r["sharpe"] > 0), None)
        return {
            "grid": rows,
            "baseline": baseline,
            "breakeven_cost_bps": (
                first_positive["cost_bps_one_way"] if first_positive else None
            ),
            "meta": _frame_meta(frame),
            "oos_start": oos_start,
            "note": (
                "Recomputed live on the current data bundle with the same "
                "strategy definition — this is NOT the published research card."
            ),
        }

    return _cached(
        f"costs:{','.join(str(c) for c in costs_bps)}:{oos_start}:"
        f"{None if close is None else close.shape[1]}",
        _compute,
    )


# ---------------------------------------------------------------------------
# 3. Strategy correlation
# ---------------------------------------------------------------------------

#: Candidate families compared for diversification.  Keys map to signal builders
#: defined in ``research_live.strategies`` plus MomReM itself.
CORRELATION_FAMILIES = (
    "momrem",
    "dual_ma",
    "ma_cross",
    "donchian",
    "supertrend",
    "supertrend_fast",
    "ts_momentum",
    "rsi_rev",
    "bollinger_rev",
    "momentum_cs_ls",
    "reversal_cs_ls",
)


def _family_returns(close: pd.DataFrame, families: Iterable[str]) -> dict[str, pd.Series]:
    """Daily net returns per family, computed on the shared panel."""
    from research_live.strategies import (
        strat_bollinger_rev,
        strat_donchian,
        strat_dual_ma,
        strat_ma_cross,
        strat_momentum_cs_ls,
        strat_reversal_cs_ls,
        strat_rsi_rev,
        strat_supertrend,
        strat_supertrend_fast,
        strat_ts_momentum,
    )

    panel = wide("high")
    high = panel.reindex(index=close.index, columns=close.columns)
    low = wide("low").reindex(index=close.index, columns=close.columns)
    open_ = wide("open").reindex(index=close.index, columns=close.columns)

    builders: dict[str, Any] = {
        "dual_ma": lambda: strat_dual_ma(close, high, low, open_),
        "ma_cross": lambda: strat_ma_cross(close, high, low, open_),
        "donchian": lambda: strat_donchian(close, high, low, open_),
        # Two parameterisations of the same indicator, on purpose: if these two
        # correlate near 1.0 then adding a second SuperTrend buys no
        # diversification, which is exactly the question this panel answers.
        "supertrend": lambda: strat_supertrend(close, high, low, open_),
        "supertrend_fast": lambda: strat_supertrend_fast(close, high, low, open_),
        "ts_momentum": lambda: strat_ts_momentum(close, high, low, open_),
        "rsi_rev": lambda: strat_rsi_rev(close, high, low, open_),
        "bollinger_rev": lambda: strat_bollinger_rev(close, high, low, open_),
    }
    long_short = {"momentum_cs_ls", "reversal_cs_ls"}
    out: dict[str, pd.Series] = {}
    for family in families:
        try:
            if family == "momrem":
                targets = momrem_targets(close)
                ls = False
            elif family in long_short:
                fn = (
                    strat_momentum_cs_ls
                    if family == "momentum_cs_ls"
                    else strat_reversal_cs_ls
                )
                targets = fn(close, high, low, open_).shift(1).fillna(0.0)
                ls = True
            else:
                targets = builders[family]().shift(1).fillna(0.0)
                ls = False
            ret = close.pct_change(fill_method=None).fillna(0.0)
            t = targets.to_numpy(dtype=float)
            r = ret.reindex_like(targets).to_numpy(dtype=float)
            if ls:
                gross = np.abs(t).sum(axis=1, keepdims=True)
                t = t / np.maximum(gross, 1e-12)
            else:
                t = np.clip(t, 0.0, 1.0)
                gross = t.sum(axis=1, keepdims=True)
                t = t * np.minimum(1.0 / np.maximum(gross, 1e-12), 1.0)
            port = (t * r).sum(axis=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                w_end = t * (1.0 + r)
            w_end = w_end / np.maximum(1.0 + port[:, None], 1e-12)
            t_next = np.vstack([t[1:], t[-1:]])
            turnover = np.abs(t_next - w_end).sum(axis=1)
            net = (1.0 + port) - turnover * MOMREM_PARAMS["cost_oneway"]
            out[family] = pd.Series(net - 1.0, index=targets.index, name=family)
        except Exception as exc:  # noqa: BLE001 - one bad family must not break the matrix
            logger.warning("family_returns_failed %s: %s", family, exc)
    return out


def strategy_correlation(
    close: pd.DataFrame | None = None,
    *,
    families: Sequence[str] = CORRELATION_FAMILIES,
    rolling_window: int = 126,
) -> dict[str, Any]:
    """Correlation matrix of candidate strategy returns + a diversification verdict."""

    def _compute() -> dict[str, Any]:
        frame = close if close is not None else strategy_frame()[0]
        returns = _family_returns(frame, families)
        if len(returns) < 2:
            return {
                "families": list(returns),
                "matrix": [],
                "error": "fewer than two families could be evaluated",
                "meta": _frame_meta(frame),
            }
        table = pd.DataFrame(returns).dropna(how="all")
        corr = table.corr()
        names = list(corr.columns)
        matrix = [
            {
                "row": row,
                "values": [
                    None if pd.isna(corr.loc[row, col]) else round(float(corr.loc[row, col]), 3)
                    for col in names
                ],
            }
            for row in names
        ]
        upper = [
            float(corr.loc[a, b])
            for i, a in enumerate(names)
            for b in names[i + 1 :]
            if not pd.isna(corr.loc[a, b])
        ]
        avg = float(np.mean(upper)) if upper else 0.0
        worst_pair = max(
            (
                (a, b, float(corr.loc[a, b]))
                for i, a in enumerate(names)
                for b in names[i + 1 :]
                if not pd.isna(corr.loc[a, b])
            ),
            key=lambda triple: triple[2],
            default=None,
        )
        # rolling correlation of each family vs MomReM
        rolling: dict[str, list[dict[str, Any]]] = {}
        if "momrem" in table.columns:
            for name in names:
                if name == "momrem":
                    continue
                series = table[name].rolling(rolling_window).corr(table["momrem"])
                rolling[name] = [
                    {"date": str(idx.date()), "corr": None if pd.isna(v) else round(float(v), 3)}
                    for idx, v in series.iloc[::5].items()
                ]
        verdict = (
            "DUPLICATIVE"
            if avg >= 0.80
            else "SOME OVERLAP"
            if avg >= 0.55
            else "DIVERSIFYING"
        )
        # The average hides the structure.  A book can average 0.5 and still
        # contain a 0.998 pair — two strategies that are the same bet twice.
        # Escalate on the worst pair so that case can never be reported as
        # "genuinely spreads the risk".
        if worst_pair and worst_pair[2] >= 0.95 and verdict == "DIVERSIFYING":
            verdict = "SOME OVERLAP"
        note = {
            "DUPLICATIVE": (
                "These families move together — running more than one is the same "
                "bet several times over, not diversification."
            ),
            "SOME OVERLAP": (
                "Meaningful overlap. Size the combination as one risk budget, not "
                "as independent strategies."
            ),
            "DIVERSIFYING": (
                "Low average pairwise correlation — a combination genuinely spreads "
                "the risk."
            ),
        }[verdict]
        if worst_pair and worst_pair[2] >= 0.90:
            note += (
                f" Note: {worst_pair[0]} and {worst_pair[1]} correlate at "
                f"{worst_pair[2]:.2f} — treat those two as one position, whatever "
                "the average says."
            )
        return {
            "families": names,
            "matrix": matrix,
            "average_pairwise": round(avg, 3),
            "worst_pair": (
                {"a": worst_pair[0], "b": worst_pair[1], "corr": round(worst_pair[2], 3)}
                if worst_pair
                else None
            ),
            "rolling_window_days": rolling_window,
            "rolling_vs_momrem": rolling,
            "verdict": verdict,
            "verdict_note": note,
            "meta": _frame_meta(frame),
        }

    return _cached(
        f"corr:{','.join(families)}:{None if close is None else close.shape[1]}",
        _compute,
    )


# ---------------------------------------------------------------------------
# 4. Backtest-vs-live divergence
# ---------------------------------------------------------------------------


def _divergence_preview(
    points: Sequence[dict[str, Any]],
    *,
    initial_capital: float | None,
    expected_cagr: float,
    expected_vol: float,
    label: str,
    reason: str,
) -> dict[str, Any]:
    """Draw the expected cone and the actual equity even before a verdict exists.

    The statistical verdict (tracking error, z-score) genuinely needs two
    sessions.  The *overlay* does not: the expected path is implied by the
    published CAGR/vol and can be drawn from the very first mark.  Returning an
    empty payload here made the tracker's most valuable output invisible on day
    one, which is exactly when a new user decides whether to trust it.
    """
    mu_annual = math.log1p(expected_cagr)
    start = float(initial_capital or (points[0]["equity"] if points else 1_000_000.0))

    def _row(fraction_of_year: float, actual: float | None) -> dict[str, Any]:
        expected = start * math.exp(mu_annual * fraction_of_year)
        sigma = expected_vol * math.sqrt(max(fraction_of_year, 0.0))
        return {
            "actual": None if actual is None else round(float(actual), 2),
            "expected": round(expected, 2),
            "band1_hi": round(expected * math.exp(sigma), 2),
            "band1_lo": round(expected * math.exp(-sigma), 2),
            "band2_hi": round(expected * math.exp(2 * sigma), 2),
            "band2_lo": round(expected * math.exp(-2 * sigma), 2),
        }

    series: list[dict[str, Any]] = []
    if points:
        stamps = pd.to_datetime(
            [str(p.get("timestamp") or p.get("date")) for p in points], utc=True
        )
        values = np.asarray([float(p["equity"]) for p in points], dtype=float)
        # keep the chart legible without hiding the shape
        step = max(1, len(points) // 160)
        picks = list(range(0, len(points), step))
        if picks[-1] != len(points) - 1:
            picks.append(len(points) - 1)
        origin = stamps[0]
        for i in picks:
            elapsed_years = (stamps[i] - origin).total_seconds() / (365.25 * 86400.0)
            row = _row(elapsed_years, values[i])
            row["date"] = str(stamps[i].date())
            row["time"] = stamps[i].strftime("%H:%M")
            series.append(row)
        last = series[-1]
        summary = {
            "days_observed": 0,
            "start_equity": round(start, 2),
            "actual_equity": last["actual"],
            "expected_equity": last["expected"],
            "actual_return_pct": round((last["actual"] / start - 1.0) * 100.0, 3)
            if last["actual"] is not None
            else None,
            "expected_return_pct": round((last["expected"] / start - 1.0) * 100.0, 3),
            "gap_pct": round(
                (last["actual"] / start - last["expected"] / start) * 100.0, 3
            )
            if last["actual"] is not None
            else None,
            "tracking_error": None,
            "z_score": None,
        }
    else:
        # nothing recorded yet: show one quarter of the cone so the comparison
        # is still concrete
        horizon_sessions = 63
        for i in range(0, horizon_sessions + 1, 3):
            row = _row(i / float(TRADING_DAYS), None)
            row["date"] = f"T+{i}"
            row["time"] = ""
            series.append(row)
        last = series[-1]
        summary = {
            "days_observed": 0,
            "start_equity": round(start, 2),
            "actual_equity": None,
            "expected_equity": last["expected"],
            "actual_return_pct": None,
            "expected_return_pct": round((last["expected"] / start - 1.0) * 100.0, 3),
            "gap_pct": None,
            "tracking_error": None,
            "z_score": None,
        }

    return {
        "ready": False,
        "state": "AWAITING SESSIONS",
        "reason": reason,
        "advice": (
            "The overlay is live. The tracking-error and z-score verdicts start "
            "once a second trading session is recorded."
        ),
        "points": len(points),
        "series": series,
        "summary": summary,
        "assumptions": {
            "label": label,
            "expected_cagr": expected_cagr,
            "expected_vol": expected_vol,
            "mu_daily": round((1.0 + expected_cagr) ** (1.0 / TRADING_DAYS) - 1.0, 6),
            "sigma_daily": round(expected_vol / math.sqrt(TRADING_DAYS), 6),
            "note": (
                "Expected path is the statistical expectation from the published OOS "
                "CAGR/vol, not a replay of the historical curve."
            ),
        },
    }


def divergence_report(
    equity_points: Sequence[dict[str, Any]],
    *,
    initial_capital: float | None = None,
    expected_cagr: float = 0.193,
    expected_vol: float = 0.20,
    label: str = "MomReM OOS",
) -> dict[str, Any]:
    """Compare realised paper equity against the backtest's expected path.

    The expected path is the *statistical* expectation implied by the published
    OOS numbers (CAGR and annualised vol), not a copy of the historical curve:
    that is the only honest forward-looking benchmark, because the future does
    not replay 2019–2026 bar for bar.

    Outputs
    -------
    ``expected`` / ``actual``   aligned series for the overlay chart, each with
                                ±1σ and ±2σ cones on the expected path.
    ``tracking_error``          annualised std of (actual − expected) daily returns.
    ``z_score``                 cumulative log-gap in units of expected σ.  The
                                early-warning number: |z| > 1 deserves a look,
                                |z| > 2 means investigate before trading more.
    """
    points = [p for p in equity_points if p.get("equity") is not None]
    if len(points) < 2:
        return _divergence_preview(
            points,
            initial_capital=initial_capital,
            expected_cagr=expected_cagr,
            expected_vol=expected_vol,
            label=label,
            reason=(
                f"only {len(points)} equity snapshot(s) recorded — the tracker needs at "
                "least two. Start the paper monitor and let it run a few sessions."
            ),
        )
    stamps = pd.to_datetime([str(p.get("timestamp") or p.get("date")) for p in points], utc=True)
    values = np.array([float(p["equity"]) for p in points], dtype=float)
    # one mark per trading day: keep the last snapshot of each IST calendar day
    frame = pd.DataFrame({"stamp": stamps, "equity": values}).set_index("stamp")
    daily = frame["equity"].resample("1D").last().dropna()
    if len(daily) < 2:
        # Still worth drawing: the expected cone needs no history, and seeing
        # where the account actually sits against it is the whole point.  Only
        # the statistical verdict is withheld until a second session exists.
        return _divergence_preview(
            points,
            initial_capital=initial_capital,
            expected_cagr=expected_cagr,
            expected_vol=expected_vol,
            label=label,
            reason=(
                "equity snapshots all fall on the same day — the overlay below is "
                "drawn, but the tracking-error and z-score verdicts need a second "
                "session."
            ),
        )
    start_equity = float(initial_capital or daily.iloc[0])
    days = np.arange(len(daily), dtype=float)
    mu_daily = (1.0 + expected_cagr) ** (1.0 / TRADING_DAYS) - 1.0
    sigma_daily = expected_vol / math.sqrt(TRADING_DAYS)

    expected = start_equity * (1.0 + mu_daily) ** days
    sigma_cum = sigma_daily * np.sqrt(np.maximum(days, 1.0))
    log_expected = np.log(expected / start_equity)

    actual = daily.to_numpy(dtype=float)
    actual_ret = np.diff(actual) / np.maximum(actual[:-1], 1e-9)
    expected_ret = np.full(len(actual_ret), mu_daily)
    diff = actual_ret - expected_ret
    tracking_error = (
        float(np.std(diff, ddof=1) * math.sqrt(TRADING_DAYS)) if len(diff) > 1 else None
    )
    gap = math.log(max(actual[-1], 1e-9) / start_equity) - log_expected[-1]
    denom = sigma_daily * math.sqrt(max(days[-1], 1.0))
    z = float(gap / denom) if denom > 0 else 0.0

    if abs(z) >= 2.0:
        state = "INVESTIGATE"
        advice = (
            "Live is more than 2σ away from what the backtest implies. Stop adding "
            "capital until you know whether this is a data problem, an execution "
            "assumption problem, or a genuine regime shift."
        )
    elif abs(z) >= 1.0:
        state = "WATCH"
        advice = (
            "Live is 1–2σ from expectation — inside normal noise, but start logging "
            "the daily gap and check the quote source and fill assumptions."
        )
    else:
        state = "ON TRACK"
        advice = "Live is tracking the backtest's expected path within 1σ."

    series = [
        {
            "date": str(idx.date()),
            "actual": round(float(a), 2),
            "expected": round(float(e), 2),
            "band1_hi": round(float(e * math.exp(sigma_cum[i])), 2),
            "band1_lo": round(float(e * math.exp(-sigma_cum[i])), 2),
            "band2_hi": round(float(e * math.exp(2 * sigma_cum[i])), 2),
            "band2_lo": round(float(e * math.exp(-2 * sigma_cum[i])), 2),
        }
        for i, (idx, a, e) in enumerate(zip(daily.index, actual, expected))
    ]
    return {
        "ready": True,
        "state": state,
        "advice": advice,
        "series": series,
        "summary": {
            "days_observed": int(len(daily)),
            "start_equity": round(start_equity, 2),
            "actual_equity": round(float(actual[-1]), 2),
            "expected_equity": round(float(expected[-1]), 2),
            "actual_return_pct": round((float(actual[-1]) / start_equity - 1.0) * 100.0, 3),
            "expected_return_pct": round((float(expected[-1]) / start_equity - 1.0) * 100.0, 3),
            "gap_pct": round(
                (float(actual[-1]) / start_equity - float(expected[-1]) / start_equity) * 100.0, 3
            ),
            "tracking_error": None if tracking_error is None else round(tracking_error, 4),
            "z_score": round(z, 3),
        },
        "assumptions": {
            "label": label,
            "expected_cagr": expected_cagr,
            "expected_vol": expected_vol,
            "mu_daily": round(mu_daily, 6),
            "sigma_daily": round(sigma_daily, 6),
            "note": (
                "Expected path is the statistical expectation from the published OOS "
                "CAGR/vol, not a replay of the historical curve."
            ),
        },
    }


# ---------------------------------------------------------------------------
# 5. Position sizing / risk of ruin
# ---------------------------------------------------------------------------


def realized_vol(close: pd.DataFrame, window: int = 60) -> pd.Series:
    """Annualised realised volatility per symbol."""
    ret = close.pct_change(fill_method=None)
    return (ret.rolling(window).std() * math.sqrt(TRADING_DAYS)).iloc[-1]


def position_sizing(
    *,
    capital: float,
    basket: Sequence[dict[str, Any]],
    close: pd.DataFrame | None = None,
    target_vol: float = 0.18,
    max_position_weight: float = 0.15,
    kelly_fraction: float = 0.25,
    win_rate: float | None = None,
    avg_win: float | None = None,
    avg_loss: float | None = None,
    rebalance_days: int = 20,
    monte_carlo_paths: int = 2000,
    horizon_years: int = 3,
    ruin_drawdown: float = 0.35,
    seed: int = 20260901,
) -> dict[str, Any]:
    """Vol-targeted sizing, Kelly fraction, and a bootstrap risk-of-ruin estimate.

    * **Vol targeting** — each name gets a slice inversely proportional to its own
      realised volatility, so a 40%-vol smallcap never gets the same rupees as a
      15%-vol largecap.  The book is then scaled so portfolio vol ≈ ``target_vol``.
    * **Kelly** — ``f* = W − (1−W)/R`` capped at ``max_position_weight``; the
      headline number is a *fraction* of full Kelly because full Kelly assumes you
      know the true odds (you don't).
    * **Risk of ruin** — bootstrap the strategy's own daily return distribution
      and count how often equity falls ``ruin_drawdown`` from its peak within the
      horizon.
    """

    def _compute() -> dict[str, Any]:
        rows = []
        vols: dict[str, float] = {}
        if close is not None and basket:
            vols = {
                str(sym): float(v)
                for sym, v in realized_vol(close).items()
                if np.isfinite(v) and v > 0
            }
        default_vol = float(np.median(list(vols.values()))) if vols else 0.25
        inv = []
        for item in basket:
            symbol = str(item["symbol"]).upper()
            vol = vols.get(symbol, default_vol)
            inv.append(1.0 / max(vol, 1e-6))
        total_inv = float(sum(inv)) or 1.0
        raw_weights = [w / total_inv for w in inv]
        scale = 1.0
        if max(raw_weights) > max_position_weight:
            scale = max_position_weight / max(raw_weights)
        weights = [w * scale for w in raw_weights]

        for item, weight in zip(basket, weights):
            symbol = str(item["symbol"]).upper()
            vol = vols.get(symbol, default_vol)
            price = float(item.get("last_close") or item.get("price") or 0.0)
            amount = capital * weight
            qty = int(amount // price) if price > 0 else 0
            rows.append(
                {
                    "symbol": symbol,
                    "realised_vol_pct": round(vol * 100.0, 2),
                    "equal_weight_pct": round(100.0 / max(len(basket), 1), 2),
                    "vol_target_weight_pct": round(weight * 100.0, 2),
                    "last_close": round(price, 2),
                    "equal_weight_qty": (
                        int((capital / max(len(basket), 1)) // price) if price > 0 else 0
                    ),
                    "vol_target_qty": qty,
                    "invested": round(qty * price, 2),
                }
            )

        # Kelly needs a real payoff ratio.  The old defaults (avg_win=avg_loss=1.0)
        # made R=1.0, so f* = W − (1−W) went negative for any win rate below 50%
        # and the "recommended" size was always 0% — a number that looked
        # computed but never was.  Derive W and R from the strategy's own
        # rebalance-period returns unless the caller supplies them explicitly.
        # `_compute` is a closure: assigning to the parameter names here would
        # make them unbound locals and shadow the arguments, so work on copies.
        w_rate = win_rate
        a_win = avg_win
        a_loss = avg_loss
        daily = None
        sim_targets = None
        odds_source = "defaulted"
        odds_periods = 0
        if close is not None:
            sim_targets = momrem_targets(close)
            daily = (
                simulate_weights(close, sim_targets)["returns"].dropna().to_numpy(dtype=float)
            )
            if len(daily) > 3 * rebalance_days:
                periods = [
                    float(np.prod(1.0 + daily[i : i + rebalance_days]) - 1.0)
                    for i in range(0, len(daily) - rebalance_days + 1, rebalance_days)
                ]
                wins = [p for p in periods if p > 0]
                losses = [abs(p) for p in periods if p < 0]
                if wins and losses:
                    odds_source = "measured"
                    odds_periods = len(periods)
                    if w_rate is None:
                        w_rate = len(wins) / float(len(periods))
                    if a_win is None:
                        a_win = float(np.mean(wins))
                    if a_loss is None:
                        a_loss = float(np.mean(losses))

        # last-resort defaults only if there was nothing to measure
        if w_rate is None:
            w_rate = 0.4798
        if a_win is None:
            a_win = 1.0
        if a_loss is None:
            a_loss = 1.0

        odds = a_win / a_loss if a_loss > 0 else 0.0
        kelly_full = w_rate - (1.0 - w_rate) / odds if odds > 0 else 0.0
        kelly_full = max(0.0, min(kelly_full, 1.0))
        kelly_used = kelly_full * kelly_fraction

        # risk of ruin by bootstrapping the strategy's own daily returns
        risk_of_ruin: float | None = None
        ruin_detail: dict[str, Any] = {}
        if daily is not None and len(daily) > 60:
            rng = np.random.default_rng(seed)
            steps = int(TRADING_DAYS * horizon_years)
            draws = rng.choice(daily, size=(monte_carlo_paths, steps))
            equity = np.cumprod(1.0 + draws, axis=1)
            peak = np.maximum.accumulate(equity, axis=1)
            dd = equity / peak - 1.0
            ruined = (dd.min(axis=1) <= -ruin_drawdown).mean()
            risk_of_ruin = float(ruined)
            ruin_detail = {
                "bootstrap_days": int(len(daily)),
                "paths": int(monte_carlo_paths),
                "horizon_years": horizon_years,
                "ruin_threshold_pct": round(ruin_drawdown * 100.0, 1),
                "median_max_dd_pct": round(float(np.median(dd.min(axis=1))) * 100.0, 2),
                "p90_max_dd_pct": round(
                    float(np.percentile(dd.min(axis=1), 10)) * 100.0, 2
                ),
                "median_terminal_multiple": round(float(np.median(equity[:, -1])), 3),
                "p10_terminal_multiple": round(
                    float(np.percentile(equity[:, -1], 10)), 3
                ),
            }

        book_vol = None
        if rows:
            book_vol = math.sqrt(
                sum((r["vol_target_weight_pct"] / 100.0 * r["realised_vol_pct"] / 100.0) ** 2
                    for r in rows)
            )  # zero-correlation lower bound
        return {
            "capital": float(capital),
            "target_vol_pct": round(target_vol * 100.0, 1),
            "max_position_weight_pct": round(max_position_weight * 100.0, 1),
            "rows": rows,
            "total_invested": round(sum(r["invested"] for r in rows), 2),
            "portfolio_vol_lower_bound_pct": (
                None if book_vol is None else round(book_vol * 100.0, 2)
            ),
            "kelly": {
                "win_rate_pct": round(w_rate * 100.0, 2),
                "odds": round(odds, 3),
                "avg_win_pct": round(a_win * 100.0, 3),
                "avg_loss_pct": round(a_loss * 100.0, 3),
                # provenance: a measured payoff ratio and a defaulted one must not
                # look the same on screen
                "source": "measured" if odds_source == "measured" else "defaulted",
                "periods_observed": odds_periods,
                "rebalance_days": rebalance_days,
                "full_kelly_pct": round(kelly_full * 100.0, 2),
                "fraction_used": kelly_fraction,
                "recommended_pct": round(kelly_used * 100.0, 2),
                "recommended_rupees": round(capital * kelly_used, 2),
                "note": (
                    "Full Kelly assumes you know the true win rate and payoff ratio. "
                    "You estimated them from ~198 trades, so a quarter-Kelly is the "
                    "sane starting point."
                ),
            },
            "risk_of_ruin_pct": (
                None if risk_of_ruin is None else round(risk_of_ruin * 100.0, 2)
            ),
            "risk_of_ruin_detail": ruin_detail,
        }

    return _cached(
        f"sizing:{capital:.0f}:{len(basket)}:{target_vol}:{max_position_weight}:"
        f"{kelly_fraction}:{None if close is None else close.shape[1]}",
        _compute,
    )
