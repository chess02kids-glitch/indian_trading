"""The real RC-1 Operations report.

The old ``/operations`` page printed a table of ``unknown`` values read from a
status JSON file that nothing in the repository ever wrote.  That is the worst
possible state for the one page whose job is to be trustworthy: an operator
could not tell a healthy system from a dead one.

This module builds the report from components that actually exist:

* **Broker health** — is an Upstox token configured, when did it expire, when
  was the last successful quote fetch.
* **Reconciliation** — the local virtual ledger's own cash/quantity audit plus a
  *position-count* reconciliation against what the strategy signal says we
  should be holding.
* **Kill switch** — a real, persisted flag from :mod:`datahub.state` that the
  paper service and the demo bot honour.
* **System health** — the heartbeats every component writes when it succeeds,
  plus data freshness from the shared :mod:`datahub` layer.

Nothing here is fabricated: a component that has never run reports ``never``,
which the UI renders as an explicit warning rather than a green light.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from datahub import state as sysstate
from datahub.panel import data_status, describe_prices_file

logger = logging.getLogger(__name__)

_STARTED_AT = time.time()

#: How old a heartbeat may be before the Operations page calls it stale.
STALE_AFTER_SECONDS = {
    "data_ingested": 3 * 86_400,
    "data_bundle_refreshed": 3 * 86_400,
    "signal_computed": 2 * 86_400,
    "quote_refreshed": 30 * 60,
    "paper_rebalance": 45 * 86_400,
    "experiment_run": 90 * 86_400,
    "reconciliation": 7 * 86_400,
    "broker_ping": 30 * 60,
}


def _age_state(name: str, age_seconds: float | None) -> str:
    if age_seconds is None:
        return "never"
    return "ok" if age_seconds <= STALE_AFTER_SECONDS.get(name, 86_400) else "stale"


# ---------------------------------------------------------------------------
# Broker health
# ---------------------------------------------------------------------------


def broker_health(market_data: Any | None = None) -> dict[str, Any]:
    """Token configuration + expiry countdown + last successful data fetch."""
    out: dict[str, Any] = {
        "configured": False,
        "mode": "PAPER_ONLY",
        "detail": "no market-data source inspected",
        "token": None,
        "last_quote_success": None,
        "last_quote_age_seconds": None,
        "last_quote_error": None,
        "state": "unknown",
    }
    if market_data is not None:
        try:
            out.update(market_data.connection_status())
        except Exception as exc:  # noqa: BLE001
            out["detail"] = f"connection status unavailable: {exc}"
        out["configured"] = bool(getattr(market_data, "access_token", ""))

    # stored token expiry, when the operator has completed the OAuth flow
    try:
        from broker.token import TokenManager

        record = TokenManager().load("upstox")
        if record is not None:
            seconds = record.seconds_until_expiry(datetime.now(UTC))
            out["token"] = {
                "broker": record.broker,
                "expires_at": record.expires_at.isoformat(),
                "seconds_until_expiry": round(seconds, 0),
                "expired": record.is_expired(datetime.now(UTC)),
                "masked": record.masked_token,
            }
    except Exception as exc:  # noqa: BLE001 - token store is optional
        logger.debug("token_status_unavailable: %s", exc)

    beats = sysstate.heartbeats()
    quote = beats.get("quote_refreshed", {})
    out["last_quote_success"] = quote.get("at")
    out["last_quote_age_seconds"] = quote.get("age_seconds")
    out["last_quote_detail"] = quote.get("detail")
    out["last_quote_error"] = (
        (quote.get("detail") or {}).get("error")
        if isinstance(quote.get("detail"), dict)
        else None
    )

    if not out["configured"]:
        out["state"] = "NOT_CONFIGURED"
        out["detail"] = out.get("detail") or (
            "no UPSTOX_ACCESS_TOKEN in the environment — the quote chain runs on "
            "clearly-labelled SIM/EOD prices"
        )
    elif quote.get("at") is None:
        out["state"] = "UNVERIFIED"
        out["detail"] = "token present but no successful quote fetch has been recorded"
    elif _age_state("quote_refreshed", quote.get("age_seconds")) == "stale":
        out["state"] = "STALE"
    else:
        out["state"] = "HEALTHY"
    if out.get("token", {}) and out["token"].get("expired"):
        out["state"] = "TOKEN_EXPIRED"
    return out


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def reconciliation(paper: Any | None, signal: dict[str, Any] | None) -> dict[str, Any]:
    """Ledger self-audit + expected-vs-actual position count from the signal."""
    out: dict[str, Any] = {
        "ledger_audit": None,
        "expected_positions": None,
        "actual_positions": None,
        "missing": [],
        "unexpected": [],
        "state": "NO_SERVICE",
        "detail": "no paper service is attached to this report",
        "checked_at": datetime.now(UTC).isoformat(),
    }
    if paper is None:
        return out
    try:
        audit = paper.audit()
    except Exception as exc:  # noqa: BLE001
        audit = {"passed": None, "error": str(exc)}
    out["ledger_audit"] = audit
    try:
        sysstate.beat("reconciliation", {"passed": audit.get("passed")})
    except Exception:  # noqa: BLE001
        pass

    try:
        positions = paper.ledger.all_positions()
    except Exception as exc:  # noqa: BLE001
        positions = []
        out["detail"] = f"could not read positions: {exc}"
    held = {
        str(p["symbol"]).upper(): int(p["quantity"])
        for p in positions
        if int(p.get("quantity") or 0) != 0
    }
    out["actual_positions"] = len(held)

    if signal is None:
        out["state"] = "NO_SIGNAL"
        out["detail"] = (
            out["detail"]
            if out["detail"] != "no paper service available"
            else (
                "strategy signal unavailable — cannot compare expected vs actual holdings"
            )
        )
        return out

    regime = str((signal.get("position") or {}).get("state", "")).upper()
    if regime == "IN_CASH":
        expected: dict[str, int] = {}
    else:
        expected = {
            str(item["symbol"]).upper(): int(item["qty"])
            for item in signal.get("basket", [])
            if int(item.get("qty") or 0) > 0
        }
    out["expected_positions"] = len(expected)
    out["missing"] = sorted(set(expected) - set(held))
    out["unexpected"] = sorted(set(held) - set(expected))
    out["expected_symbols"] = sorted(expected)
    out["held_symbols"] = sorted(held)
    out["strategy"] = signal.get("strategy", "momrem")
    out["signal_as_of"] = signal.get("as_of")

    ledger_ok = audit.get("passed") is not False
    if not held and not expected:
        out["state"] = "FLAT"
        out["detail"] = (
            "no positions held and the signal expects none "
            f"(regime: {regime or 'unknown'})"
        )
    elif not out["missing"] and not out["unexpected"] and ledger_ok:
        out["state"] = "MATCHED"
        out["detail"] = f"{len(held)} positions match the strategy target"
    elif not held and expected:
        out["state"] = "NOT_STARTED"
        out["detail"] = (
            f"the signal expects {len(expected)} positions but the paper account is "
            "flat — run a paper rebalance (or enable auto-paper) to track it"
        )
    else:
        out["state"] = "DIVERGED"
        out["detail"] = (
            f"{len(out['missing'])} expected position(s) missing, "
            f"{len(out['unexpected'])} unexpected"
        )
    if not ledger_ok:
        out["state"] = "LEDGER_MISMATCH"
        out["detail"] = (
            f"ledger audit failed: cash difference "
            f"{audit.get('cash_difference')} — inspect the CSV exports"
        )
    return out


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------


def system_health(feed: Any | None = None) -> dict[str, Any]:
    """Data freshness, heartbeats, process uptime and feed mode."""
    beats = sysstate.heartbeats()
    for name, beat in beats.items():
        beat["state"] = _age_state(name, beat.get("age_seconds"))
    try:
        data = data_status()
    except Exception as exc:  # noqa: BLE001
        data = {"available": False, "error": str(exc)}
    freshness = data.get("freshness") or {}
    feed_payload: dict[str, Any] = {"mode": "UNKNOWN", "note": "feed not started"}
    if feed is not None:
        try:
            snap = feed.snapshot()
            feed_payload = snap.get("feed", {})
        except Exception as exc:  # noqa: BLE001
            feed_payload = {"mode": "ERROR", "note": str(exc)}

    stale = [n for n, b in beats.items() if b["state"] == "stale"]
    never = [n for n, b in beats.items() if b["state"] == "never"]
    if not data.get("available"):
        overall = "DEGRADED"
    elif sysstate.is_killed():
        overall = "HALTED"
    elif stale:
        overall = "STALE"
    elif never and len(never) == len(beats):
        overall = "COLD_START"
    else:
        overall = "HEALTHY"
    return {
        "overall": overall,
        "heartbeats": beats,
        "stale": stale,
        "never": never,
        "data": {
            "available": bool(data.get("available")),
            "last_bar": freshness.get("last_bar"),
            "last_bar_age_days": freshness.get("last_bar_age_days"),
            "bundle_files": freshness.get("bundle_files"),
            "panel_symbols": (data.get("prices_info") or {}).get("symbols"),
            "universe_size": (data.get("universe") or {}).get("size"),
            "prices_parquet": describe_prices_file(),
            "source_last_update": freshness.get("source_last_update"),
        },
        "feed": feed_payload,
        "process": {
            "uptime_seconds": round(time.time() - _STARTED_AT, 1),
            "started_at": datetime.fromtimestamp(_STARTED_AT, tz=UTC).isoformat(),
            "pid": os.getpid(),
        },
        "checked_at": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------


def build_report(
    paper: Any | None = None,
    signal: dict[str, Any] | None = None,
    market_data: Any | None = None,
    feed: Any | None = None,
) -> dict[str, Any]:
    """Assemble the whole Operations payload in one call."""
    switch = sysstate.kill_switch()
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "kill_switch": switch,
        "broker_health": broker_health(market_data),
        "reconciliation": reconciliation(paper, signal),
        "system_health": system_health(feed),
        "history": sysstate.history(15),
        "scope": (
            "read-only operational report over the local virtual paper system; "
            "no broker order API is reachable from this page"
        ),
    }
    # one honest headline instead of six independent lights
    states = [
        report["kill_switch"]["armed"],
        report["broker_health"]["state"],
        report["reconciliation"]["state"],
        report["system_health"]["overall"],
    ]
    if switch["armed"]:
        report["headline"] = (
            "HALTED",
            "Kill switch is armed — all trading is blocked.",
        )
    elif states[1] in ("TOKEN_EXPIRED",):
        report["headline"] = ("ACTION", "Upstox token has expired — re-authenticate.")
    elif states[3] == "DEGRADED":
        report["headline"] = (
            "DEGRADED",
            "Price data is unavailable — fix ingestion first.",
        )
    elif states[2] in ("DIVERGED", "LEDGER_MISMATCH"):
        report["headline"] = (
            "DIVERGED",
            "Positions do not match the strategy target or the ledger audit failed.",
        )
    elif states[3] == "COLD_START":
        report["headline"] = (
            "COLD START",
            "Nothing has run yet. Start the paper monitor and let it record a session.",
        )
    elif states[3] == "STALE":
        report["headline"] = ("STALE", "One or more components have not run recently.")
    else:
        report["headline"] = ("HEALTHY", "All monitored components reported in.")
    return report
