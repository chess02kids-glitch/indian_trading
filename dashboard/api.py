"""JSON API for the unified dashboard.

One process, one data layer, one page.  Every panel in ``/`` (Overview,
Strategy, Divergence, Risk, Research, Operations) is fed by a function here, and
every function here reads from :mod:`datahub` — which is what stops the pages
from disagreeing with each other.

Design rules:

* Never raise to the HTTP layer with a bare traceback: return
  ``{"error": ..., "detail": ...}`` so the UI can show a real message.
* Never present simulated data as real.  Every payload carries the source of the
  numbers it contains.
* Cache aggressively (the panel is ~2.7M rows) but let the UI force a refresh.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    """Run a payload builder, converting failures into a displayable error."""
    try:
        result = fn(*args, **kwargs)
        return result if isinstance(result, dict) else {"value": result}
    except Exception as exc:  # noqa: BLE001 - one bad panel must not kill the page
        logger.exception("api_payload_failed %s", getattr(fn, "__name__", fn))
        return {"error": type(exc).__name__, "detail": str(exc)}


# ---------------------------------------------------------------------------
# Shared service accessors (imported lazily to keep startup cheap)
# ---------------------------------------------------------------------------


def _paper_service() -> Any:
    from dashboard.server import get_paper_service

    return get_paper_service()


def _live_feed() -> Any:
    from dashboard.server import get_live_feed

    return get_live_feed()


def _signal(capital: float) -> dict[str, Any] | None:
    try:
        from dashboard.strategy_dashboard import compute_momrem_signal

        return compute_momrem_signal(capital)
    except Exception as exc:  # noqa: BLE001
        logger.warning("signal_unavailable: %s", exc)
        return None


def _equity_points(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise ledger equity snapshots to ``{timestamp, equity}``.

    The ledger column is ``recorded_at``; older rows written by other paths use
    ``timestamp``.  Accept either rather than raising a KeyError into the UI.
    """
    out: list[dict[str, Any]] = []
    for row in history:
        stamp = row.get("recorded_at") or row.get("timestamp") or row.get("created_at")
        equity = row.get("equity")
        if stamp is None or equity is None:
            continue
        out.append({"timestamp": str(stamp), "equity": float(equity)})
    return out


def _frame():
    from datahub.panel import strategy_frame

    return strategy_frame()


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------


def data_status_payload(refresh: bool = False) -> dict[str, Any]:
    from datahub.panel import data_status, describe_prices_file
    from datahub.universe import status as universe_status

    def _build() -> dict[str, Any]:
        payload = data_status(refresh=refresh)
        payload["prices_parquet"] = describe_prices_file()
        payload["expansion"] = universe_status()
        return payload

    return _safe(_build)


def signal_payload(capital: float = 100_000.0) -> dict[str, Any]:
    """Strategy signal + the regime lens + position sizing for the same basket."""
    from datahub.analytics import position_sizing, regime_summary

    def _build() -> dict[str, Any]:
        signal = _signal(capital)
        if signal is None:
            return {
                "error": "SIGNAL_UNAVAILABLE",
                "detail": "compute_momrem_signal failed",
            }
        close, meta = _frame()
        sizing = position_sizing(
            capital=capital,
            basket=signal["basket"],
            close=close,
            max_position_weight=0.15,
        )
        return {
            "signal": signal,
            "regime": regime_summary(close),
            "sizing": sizing,
            "universe": meta,
        }

    return _safe(_build)


def divergence_payload(capital: float = 100_000.0) -> dict[str, Any]:
    """Backtest expectation vs realised paper equity, with a tracking error."""
    from datahub.analytics import divergence_report

    def _build() -> dict[str, Any]:
        paper = _paper_service()
        settings = paper.ledger.settings()
        history = paper.ledger.equity_history(limit=2000)
        initial = float(settings.get("initial_capital") or capital)
        points = _equity_points(history)
        report = divergence_report(points, initial_capital=initial)
        report["account"] = {
            "initial_capital": initial,
            "running": bool(settings.get("running")),
            "data_mode": str(settings.get("data_mode")),
            "equity_points": len(points),
            "first_point": points[0]["timestamp"] if points else None,
            "last_point": points[-1]["timestamp"] if points else None,
        }
        return report

    return _safe(_build)


def cost_sensitivity_payload() -> dict[str, Any]:
    from datahub.analytics import cost_sensitivity

    def _build() -> dict[str, Any]:
        close, meta = _frame()
        payload = cost_sensitivity(close)
        payload["universe"] = {
            "size": meta["size"],
            "research_parity_symbols": meta["research_parity_symbols"],
        }
        return payload

    return _safe(_build)


def correlation_payload() -> dict[str, Any]:
    from datahub.analytics import strategy_correlation

    def _build() -> dict[str, Any]:
        close, _meta = _frame()
        return strategy_correlation(close)

    return _safe(_build)


def sizing_payload(capital: float = 100_000.0) -> dict[str, Any]:
    from datahub.analytics import position_sizing

    def _build() -> dict[str, Any]:
        signal = _signal(capital)
        basket = (signal or {}).get("basket", [])
        close, _meta = _frame()
        payload = position_sizing(capital=capital, basket=basket, close=close)
        payload["as_of"] = (signal or {}).get("as_of")
        return payload

    return _safe(_build)


def regime_payload() -> dict[str, Any]:
    from datahub.analytics import regime_summary, regime_tagged_equity
    from datahub.panel import ROOT

    def _build() -> dict[str, Any]:
        import pandas as pd

        from datahub.analytics import regime_series

        close, _meta = _frame()
        summary = regime_summary(close)
        # The real market proxy and its moving average, downsampled for the
        # sparkline.  Plotting the equity curve here and labelling it "proxy"
        # would have been a lie dressed up as a chart.
        series = regime_series(close)
        step = max(1, len(series) // 400)
        sampled = series.iloc[::step]
        # always keep the newest bar so the sparkline's end agrees with the
        # reported current regime (strided sampling otherwise drops it)
        if len(sampled) and sampled.index[-1] != series.index[-1]:
            sampled = pd.concat([sampled, series.iloc[[-1]]])

        def _num(value: Any) -> float | None:
            """JSON has no NaN/Infinity.  Emit null or break strict parsers."""
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return round(number, 4) if number == number else None

        proxy_series = [
            {
                "date": str(idx.date()),
                "proxy": _num(row["proxy"]),
                "sma": _num(row["ma"]),
                "label": str(row["label"]),
                "filter": str(row["filter"]),
            }
            for idx, row in sampled.iterrows()
        ]
        equity_csv = ROOT / "research_live" / "deliverables" / "equity.csv"
        tagged: list[dict[str, Any]] = []
        if equity_csv.is_file():
            curve = pd.read_csv(equity_csv, parse_dates=["date"])
            dates = [str(d.date()) for d in curve["date"]]
            tagged = regime_tagged_equity(dates, close)
        return {
            "summary": summary,
            "proxy_series": proxy_series,
            "equity_curve": [
                {"date": str(d.date()), "equity": float(v)}
                for d, v in zip(curve["date"], curve["equity"])
            ]
            if equity_csv.is_file()
            else [],
            "regime_by_date": tagged,
        }

    return _safe(_build)


def operations_payload() -> dict[str, Any]:
    from dashboard.operations import build_report

    def _build() -> dict[str, Any]:
        paper = _paper_service()
        feed = None
        try:
            feed = _live_feed()
        except Exception:  # noqa: BLE001 - the feed is heavy; ops must still render
            feed = None
        settings = paper.ledger.settings()
        capital = float(settings.get("initial_capital") or 100_000.0)
        return build_report(
            paper=paper,
            signal=_signal(capital),
            market_data=paper.market_data,
            feed=feed,
        )

    return _safe(_build)


def overview_payload(capital: float = 100_000.0) -> dict[str, Any]:
    """The one-screen answer: what is the system doing and what should I do."""
    from dashboard.operations import build_report
    from datahub.analytics import divergence_report

    def _build() -> dict[str, Any]:
        paper = _paper_service()
        settings = paper.ledger.settings()
        initial = float(settings.get("initial_capital") or capital)
        signal = _signal(initial)
        feed = None
        try:
            feed = _live_feed()
        except Exception:  # noqa: BLE001
            feed = None
        ops = build_report(
            paper=paper, signal=signal, market_data=paper.market_data, feed=feed
        )
        history = paper.ledger.equity_history(limit=2000)
        divergence = divergence_report(_equity_points(history), initial_capital=initial)
        return {
            "operations": ops,
            "signal": signal,
            "divergence": divergence,
            "paper": {
                "initial_capital": initial,
                "running": bool(settings.get("running")),
                "quote_health": paper._quote_health(settings),
                "positions": len(paper.ledger.all_positions()),
            },
        }

    return _safe(_build)


def research_check_payload() -> dict[str, Any]:
    """Honest comparison of the published card against a fresh recomputation."""

    def _build() -> dict[str, Any]:
        import pandas as pd

        from dashboard.strategy_dashboard import MOMREM_CARD
        from datahub.analytics import (
            cost_sensitivity,
            momrem_targets,
            performance,
            simulate_weights,
        )

        close, meta = _frame()
        targets = momrem_targets(close)
        sim = simulate_weights(close, targets)
        oos = sim["returns"].index >= pd.Timestamp("2019-01-01")
        recomputed_oos = performance(sim["returns"][oos], sim["equity"][oos])
        recomputed_full = performance(sim["returns"], sim["equity"])
        costs = cost_sensitivity(close)
        published = MOMREM_CARD
        return {
            "published": {
                "source": "research_live/deliverables/STRATEGY_REPORT.md",
                "universe": "~552 liquid names (median traded value >= Rs 10M, >=8y history)",
                "window": "2010-01-01 to 2026-06-30",
                "oos": published["oos"],
                "full": published["full"],
                "validation": published["validation"],
            },
            "recomputed": {
                "source": "datahub.analytics.momrem_targets on the current data bundle",
                "universe": f"{meta['size']} names from the current bundle",
                "research_parity_symbols": meta["research_parity_symbols"],
                "window": f"{meta['start']} to {meta['as_of']}",
                "oos": {k: round(v, 4) for k, v in recomputed_oos.items()},
                "full": {k: round(v, 4) for k, v in recomputed_full.items()},
            },
            "cost_grid": costs["grid"],
            "note": (
                "The published card was produced by research_live/mom_overlay.py, whose "
                "rebalance grid never cleared names that dropped out of the top-20. "
                "ffill() therefore carried them forward forever and the backtest held "
                "~272 names on average instead of 20 — closer to an equal-weight broad "
                "portfolio with a regime filter than to a top-20 momentum book. That bug "
                "is now fixed; the recomputed column is the strategy implemented as the "
                "card describes it."
            ),
        }

    return _safe(_build)
