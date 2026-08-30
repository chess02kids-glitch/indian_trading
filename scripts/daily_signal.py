#!/usr/bin/env python3
"""Daily MomReM signal CLI.

Computes the validated MomReM strategy's live signal from the freshest data
in ``data/clean/eod2_data``, prints a human-readable summary, optionally saves
``var/signal/latest.json``, and optionally sends a Telegram alert (via the
existing ``observability.alerts`` service when ``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID`` are set).

Usage:
    python scripts/daily_signal.py                          # print only
    python scripts/daily_signal.py --capital 200000 --save  # save JSON too
    python scripts/daily_signal.py --telegram               # + Telegram alert
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.strategy_dashboard import (  # noqa: E402
    build_signal_payload,
    compute_momrem_signal,
)


def _fmt_basket(sig: dict) -> str:
    lines = [
        f"{'SYMBOL':<16}{'20d MOM':>9}{'WEIGHT':>8}{'CLOSE ₹':>10}{'QTY':>8}{'INVESTED ₹':>12}"
    ]
    for b in sig["basket"]:
        qty = "—" if b["qty"] == 0 else f"{b['qty']:,}"
        lines.append(
            f"{b['symbol']:<16}{b['mom20_pct']:>+8.2f}%{b['weight_pct']:>7.1f}%"
            f"{b['last_close']:>10,.2f}{qty:>8}{b['spent']:>12,.0f}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily MomReM signal")
    parser.add_argument(
        "--capital", type=float, default=100_000.0, help="deployable capital in ₹"
    )
    parser.add_argument(
        "--save", action="store_true", help="save var/signal/latest.json"
    )
    parser.add_argument(
        "--telegram", action="store_true", help="send Telegram alert if configured"
    )
    args = parser.parse_args()

    sig = compute_momrem_signal(args.capital)
    regime = sig["regime"]
    pos = sig["position"]

    print("=" * 62)
    print(
        "MomReM — Momentum + Market-Regime Filter  (VALIDATED, deflated Sharpe 0.999)"
    )
    print("=" * 62)
    stale_note = (
        f"STALE {sig['stale_days']}d — run fetch_data.py"
        if not sig["fresh"]
        else "FRESH"
    )
    print(f"As of            : {sig['as_of']}  ({stale_note})")
    print(
        f"Market regime    : {regime['state']}  (proxy {regime['proxy']:.4f} vs 100d SMA {regime['sma100']:.4f}, {regime['proxy_vs_sma_pct']:+.2f}%)"
    )
    print(f"Strategy position: {pos['state']}  ({pos['note']})")
    print(f"Last rebalance   : {sig['last_rebalance']}   Next: {sig['next_rebalance']}")
    print(
        f"Since rebalance  : {sig['return_since_rebalance_pct']:+.2f}% (est., net of entry cost)"
    )
    br = sig["breadth"]
    print(
        f"Breadth          : {br['above_20d_sma_pct']}% above 20d SMA · adv/dec 5d {br['advancers_5d']}/{br['decliners_5d']} of {br['universe_size']}"
    )
    print("-" * 62)

    if sig["basket"]:
        print(
            f"BASKET (top-{len(sig['basket'])} by 20d momentum, equal weight, capital ₹{args.capital:,.0f})"
        )
        print(_fmt_basket(sig))
        unaffordable = [b["symbol"] for b in sig["basket"] if b["qty"] == 0]
        if unaffordable:
            slice_amt = args.capital / len(sig["basket"])
            print(
                f"Note: {', '.join(unaffordable)} show '—' — 1 share costs more than the "
                f"₹{slice_amt:,.0f} per-name slice; raise capital to include them."
            )
        print(f"\nNotional ₹{sig['basket_notional']:,.0f} · cash ₹{sig['cash']:,.0f}")
    else:
        print(
            "NO BASKET — regime filter is OFF (cash). Wait for the proxy to close above its 100-day SMA."
        )

    if not sig["fresh"]:
        print(
            "\nWARNING: data is stale. Run `python fetch_data.py` before acting on this signal."
        )

    if args.save:
        out = ROOT / "var" / "signal" / "latest.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = build_signal_payload(args.capital)
        payload["generated_at"] = datetime.now(UTC).isoformat()
        out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nsaved → {out}")

    if args.telegram:
        _telegram(sig, args.capital)

    return 0


def _telegram(sig: dict, capital: float) -> None:
    """Deliver the signal via AlertService when Telegram env vars are set."""
    try:
        from observability.alerts import AlertService
    except Exception as exc:  # noqa: BLE001
        print(f"telegram skipped (alerts module unavailable): {exc}")
        return

    svc = AlertService()
    if not svc.telegram_configured():
        print("telegram skipped: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return
    regime = sig["regime"]
    top5 = ", ".join(b["symbol"] for b in sig["basket"][:5])
    message = (
        f"📊 MomReM daily signal ({sig['as_of']})\n"
        f"Regime: {regime['state']} ({regime['proxy_vs_sma_pct']:+.2f}% vs 100d SMA)\n"
        f"Position: {sig['position']['state']}\n"
        f"Basket: {len(sig['basket'])} names — {top5}\n"
        f"Capital ₹{capital:,.0f} → notional ₹{sig['basket_notional']:,.0f}\n"
        f"Next rebalance: {sig['next_rebalance']}\n"
        f"{'⚠️ STALE DATA — run fetch_data.py' if not sig['fresh'] else ''}"
    )
    svc.info("DAILY_SIGNAL", message=message)
    print("telegram alert queued")


if __name__ == "__main__":
    sys.exit(main())
