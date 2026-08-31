"""Real-data ingestion for the v0.7 baseline re-validation.

Three modes (exactly ONE of them touches the network):

Local (offline, deterministic — run in Arena or on any machine)::

    # A. Ingest the pinned source checkouts: eod2_data prices (raw +
    #    validated clean layer) and the CC-BY-4.0 point-in-time Nifty 100
    #    membership (data/universe/nifty100-pit/), then write the §7
    #    completeness report.
    python scripts/ingest_real_data.py --local \
        --eod2-dir /path/to/eod2_data \
        --membership-dir /path/to/nse-historical-membership \
        [--as-of 2026-08-25] [--window-start 2023-01-02]

    # C. Merge an operator fundamentals bundle into the research dataset
    #    and refresh the completeness report (no network).
    python scripts/ingest_real_data.py --from-bundle data/bundle

Operator (network required — Yahoo Finance; the single external-data
command of the milestone; see docs/real_data.md)::

    python scripts/ingest_real_data.py --fetch-fundamentals \
        [--output data/bundle] [--panel-symbols-file data/bundle/panel_symbols.txt]

The operator command produces data artifacts (fundamentals parquet,
independent yfinance price cross-check JSON, provenance JSON), not a
research result: research consumption happens locally in mode C and in
``scripts/run_real_data_experiment.py``.

No trading/order API, no broker credentials, read-only market data only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess  # nosec B404
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.dataset import CleanDataCatalog  # noqa: E402
from data.quality import (  # noqa: E402
    check_ohlcv_long_frame,
    detect_data_staleness,
)
from data.storage import StorageManager  # noqa: E402
from data.universe import UniverseDataset  # noqa: E402
from ingestion.eod2_adapter import (  # noqa: E402
    EOD2_SOURCE,
    Eod2SourceSpec,
    eod2_symbols_available,
    load_isin_map,
    load_meta_json,
    parse_eod2_daily_file,
    symbol_to_filename,
)
from ingestion.nse_membership_adapter import (  # noqa: E402
    NIFTY_50_INDEX_ID,
    NIFTY_100_INDEX_ID,
    NIFTY_500_INDEX_ID,
    NseMembershipSpec,
    build_pit_universe_frame,
    extract_index_rows,
    membership_fingerprint,
    parse_membership_csv,
    write_pit_universe,
)
from research import realdata  # noqa: E402

DEFAULT_AS_OF = "2026-08-25"
DEFAULT_WINDOW_START = "2023-01-02"

#: v0.7 milestone: the frozen baseline's quality screen needs these.
FUNDAMENTALS_COLUMNS = ("date", "symbol", "roe", "debt_to_equity")


# --------------------------------------------------------------------------
# local ingestion (mode A)
# --------------------------------------------------------------------------


def _git_head(repo_dir: str | Path) -> str:
    try:
        # Fixed argv (git rev-parse HEAD on a local mirror dir); no shell,
        # no untrusted input. Used only to pin the mirror commit for provenance.
        result = subprocess.run(  # nosec B603 B607
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _window_rows(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Slice normalised rows to the research window (raw-layer scope)."""
    dates = pd.to_datetime(frame["date"], errors="coerce")
    mask = dates.notna() & (dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))
    return frame.loc[mask].reset_index(drop=True)


def ingest_prices(
    pre_parsed: Mapping[str, pd.DataFrame],
    *,
    eod2_dir: Path,
    symbols: list[str],
    as_of: str,
    window_start: str,
    window_end: str,
    catalog: CleanDataCatalog,
    storage: StorageManager,
) -> dict[str, Any]:
    """Raw + validated ingestion of the eod2 price files (mode A).

    ``pre_parsed`` maps symbol -> normalised full-history frame (parsed
    once by the caller so the derived market calendar can be computed
    before the clean layer is written). The raw layer (immutable,
    ``data/raw/eod2_data/NSE/<SYM>/<YYYY>/<MM>.parquet``) stores the
    normalised source rows for the research window; the validated clean
    layer stores only rows that pass the quality gate (duplicates,
    malformed OHLC, future dates excluded + reported).
    """
    spec = Eod2SourceSpec.from_meta(
        load_meta_json(eod2_dir), commit=_git_head(eod2_dir)
    )
    available = eod2_symbols_available(eod2_dir)
    per_symbol: dict[str, Any] = {}
    combined_quality_issues: dict[str, int] = {}
    ohlc_inconsistencies: dict[str, list[str]] = {}
    combined_total = combined_accepted = 0
    for symbol in sorted(symbols):
        file_name = symbol_to_filename(symbol)
        path = eod2_dir / "daily" / file_name
        if not path.is_file():
            per_symbol[symbol] = {
                "status": "missing_in_source",
                "file": f"daily/{file_name}",
            }
            continue
        frame = pre_parsed[symbol]
        accepted, report = check_ohlcv_long_frame(
            frame, source=EOD2_SOURCE, exchange="NSE", as_of=as_of
        )
        combined_total += report.total_rows
        combined_accepted += report.accepted_rows
        for issue in report.issues:
            combined_quality_issues[issue.kind] = (
                combined_quality_issues.get(issue.kind, 0) + 1
            )
            if issue.kind == "ohlc_inconsistency" and issue.date:
                ohlc_inconsistencies.setdefault(symbol, []).append(issue.date)
        # Raw layer: normalised source rows for the research window only
        # (full history stays pinned at the source commit; documented).
        raw_frame = _window_rows(frame, window_start, window_end)
        if not raw_frame.empty:
            storage.save_historical_data(
                raw_frame, source=EOD2_SOURCE, exchange="NSE", symbol=symbol
            )
        if accepted.empty:
            per_symbol[symbol] = {
                "status": "no_valid_rows",
                "file": f"daily/{file_name}",
            }
            continue
        frame_path, metadata = catalog.write_clean(
            accepted,
            source=EOD2_SOURCE,
            exchange="NSE",
            symbol=symbol,
            require_clean=False,
        )
        per_symbol[symbol] = {
            "status": "ok",
            "file": f"daily/{file_name}",
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "clean_rows": metadata.rows,
            "clean_fingerprint": metadata.fingerprint,
            "clean_path": str(frame_path),
            "quality_issue_kinds": metadata.quality_issues,
        }
    catalog.register_duckdb(source=EOD2_SOURCE)
    for dates in ohlc_inconsistencies.values():
        dates.sort()
    return {
        "source_spec": spec.to_dict(),
        "per_symbol": per_symbol,
        "combined": {
            "total_rows": combined_total,
            "accepted_rows": combined_accepted,
            "quality_issue_counts": combined_quality_issues,
        },
        "ohlc_inconsistencies": ohlc_inconsistencies,
        "available_in_source": sorted(available),
    }


def ingest_membership(
    membership_dir: Path,
    *,
    isin_map: dict[str, str],
    as_of: str,
    retrieved_at: str,
    universe_root: Path,
) -> dict[str, Any]:
    """Build the point-in-time Nifty 50, 100, 500 universes (mode A)."""
    source_csv = (
        membership_dir / "index_history" / "data" / "index_membership_history.csv"
    )
    frame = parse_membership_csv(source_csv)

    indices = [
        (NIFTY_50_INDEX_ID, "Nifty 50", "nifty50"),
        (NIFTY_100_INDEX_ID, "Nifty 100", "nifty100"),
        (NIFTY_500_INDEX_ID, "Nifty 500", "nifty500"),
    ]

    combined_audit = {}

    # ensure parent root exists
    universe_root.mkdir(parents=True, exist_ok=True)

    for idx_id, idx_name, slug in indices:
        rows = extract_index_rows(frame, index_id=idx_id, index_name=idx_name)
        pit_frame = build_pit_universe_frame(rows, index_name=slug, isin_map=isin_map)
        u_dir = universe_root / f"{slug}-pit"
        write_pit_universe(
            u_dir,
            pit_frame,
            spec=NseMembershipSpec(
                commit=_git_head(membership_dir),
                coverage_note=(
                    "membership data through 2026-05-15 (upstream coverage); "
                    "no NSE reconstitution occurred between 2026-05-15 and the "
                    "next scheduled October 2026 review, so membership is stable "
                    "through the as-of date (verified against upstream "
                    "current_snapshot)"
                ),
            ),
            source_csv_path=source_csv,
            retrieved_at=retrieved_at,
        )
        dataset = UniverseDataset.from_dir(u_dir)
        members_as_of = dataset.members_at(slug, as_of)

        combined_audit[slug] = {
            "universe_dir": str(u_dir),
            "rows": int(pit_frame.attrs.get("kept_row_count", len(pit_frame))),
            "symbols_ever": len(dataset.all_symbols(slug)),
            "members_at_as_of": list(members_as_of),
            "members_at_as_of_count": len(members_as_of),
            "excluded_symbols": list(pit_frame.attrs.get("excluded_symbols", [])),
            "membership_fingerprint": membership_fingerprint(
                pd.read_csv(u_dir / f"{slug}.csv")
            ),
            "isin_coverage": {
                "mapped": int(pit_frame["isin"].notna().sum()),
                "unmapped": int(pit_frame["isin"].isna().sum()),
            },
        }

    return combined_audit


def requested_constituents(
    universe_root: Path, *, window_start: str, as_of: str
) -> list[str]:
    """Return all symbols that were members of Nifty 50, 100, or 500 at any point."""
    symbols = set()
    for slug in ("nifty50", "nifty100", "nifty500"):
        u_dir = universe_root / f"{slug}-pit"
        if u_dir.is_dir():
            dataset = UniverseDataset.from_dir(u_dir)
            members = realdata.requested_constituents(
                dataset, window_start=window_start, as_of=as_of
            )
            symbols.update(members)

    if not symbols:
        raise SystemExit(f"No symbols found in {universe_root}")
    return sorted(symbols)


# --------------------------------------------------------------------------
# completeness report (v0.7 §7)
# --------------------------------------------------------------------------


def build_completeness_report(
    *,
    panels: realdata.ResearchPanels | None,
    price_audit: dict[str, Any],
    membership_audit: dict[str, Any],
    as_of: str,
    window_start: str,
    bundle_dir: str | Path | None = None,
    staleness_issue: Any = None,
) -> dict[str, Any]:
    """Assemble the §7 data-completeness report (JSON-serialisable)."""
    spec = price_audit.get("source_spec", {})
    ohlc = price_audit.get("ohlc_inconsistencies", {})
    report: dict[str, Any] = {
        "as_of": as_of,
        "window_start": window_start,
        "window_end": (panels.window.end.isoformat() if panels is not None else None),
        "prices": {
            "source": spec,
            "symbols_requested": list(
                {symbol for symbol, info in price_audit["per_symbol"].items()}
            ),
            "per_symbol": price_audit["per_symbol"],
            "combined_quality": price_audit["combined"],
        },
        "adjustment": {
            "state": spec.get("adjustment_state"),
            "note": spec.get("adjustment_note"),
            "window_observations_adjusted": (
                panels.window.trading_days * len(panels.symbols)
                if panels is not None
                else None
            ),
            "ohlc_inconsistent_rows": {
                symbol: dates for symbol, dates in sorted(ohlc.items())
            },
            "ohlc_inconsistent_row_count": sum(len(v) for v in ohlc.values()),
            "ohlc_inconsistent_window_check": (
                "verified: zero OHLC-inconsistent rows inside the research "
                "window (all occurrences pre-date the window; source "
                "adjusted-series artifact, reported not repaired)"
                if panels is not None
                and all(
                    pd.Timestamp(d) < pd.Timestamp(panels.window.start)
                    for dates in ohlc.values()
                    for d in dates
                )
                else "OHLC-inconsistent rows inside the window — see list"
            ),
            "independent_crosscheck": (
                "operator bundle: data/bundle/crosscheck_yfinance.json "
                "(independent raw-close comparison, produced by the single "
                "operator external-data command)"
            ),
        },
        "universe": membership_audit,
        "panel": None,
        "fundamentals": None,
        "staleness": None,
        "limitations": [
            "eod2_data repository carries no license file; usage is a "
            "research-only mirror of NSE official public daily reports "
            "(see docs/real_data.md)",
            "daily/ series is split/bonus-adjusted (dividends NOT "
            "adjusted) per the upstream README; independent "
            "raw-adjustment cross-check requires the operator bundle "
            "(yfinance, see docs/real_data.md)",
            "the eod2 meta.json equityActions list only covers the current "
            "upcoming window (no historical 2023-2026 action history in "
            "the source); no split/bonus actions for panel symbols appear "
            "in that window",
        ],
    }
    if staleness_issue is not None:
        report["staleness"] = {
            "kind": staleness_issue.kind,
            "detail": staleness_issue.detail,
        }
    if panels is not None:
        excluded = dict(panels.excluded)
        # Sharpen "no clean data" reasons with the source-level status.
        for symbol, info in price_audit.get("per_symbol", {}).items():
            if symbol in excluded and info.get("status") == "missing_in_source":
                excluded[symbol] = (
                    "no price file in the source repository (delisted or "
                    "never published by the mirror); the source's own "
                    "point-in-time membership keeps the symbol in the "
                    "universe with a finite valid_to where applicable"
                )
        report["panel"] = {
            "trading_days": panels.window.trading_days,
            "symbols_complete": len(panels.symbols),
            "symbols_excluded": len(excluded),
            "excluded_symbols": excluded,
            "market_holidays_in_window": len(panels.market_holidays),
            "future_date_rows": price_audit["combined"]["quality_issue_counts"].get(
                "future_date", 0
            ),
            "duplicate_rows": price_audit["combined"]["quality_issue_counts"].get(
                "duplicate_row", 0
            ),
            "missing_dates_per_complete_symbol": 0,
            "coverage": realdata_frame_coverage(panels),
        }
    if bundle_dir is not None and Path(bundle_dir).is_dir():
        report["fundamentals"] = _bundle_summary(Path(bundle_dir))
    return report


def realdata_frame_coverage(panels: realdata.ResearchPanels) -> list[dict[str, Any]]:
    """Per-symbol coverage table (the §7 'observations per symbol' block)."""
    frame = panels.symbols_frame()
    return [
        {
            "symbol": str(r.symbol),
            "first_date": str(r.first_date),
            "last_date": str(r.last_date),
            "observations": int(r.observations),
        }
        for r in frame.itertuples(index=False)
    ]


def _bundle_summary(bundle_dir: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"bundle_dir": str(bundle_dir)}
    provenance_path = bundle_dir / "fundamentals_provenance.json"
    if provenance_path.is_file():
        summary["provenance"] = json.loads(provenance_path.read_text(encoding="utf-8"))
    parquet_path = bundle_dir / "fundamentals_quarterly.parquet"
    if parquet_path.is_file():
        frame = pd.read_parquet(parquet_path)
        per_symbol = (
            frame.groupby("symbol")
            .agg(rows=("roe", "size"), first=("date", "min"), last=("date", "max"))
            .reset_index()
        )
        summary["rows"] = int(len(frame))
        summary["symbols"] = int(frame["symbol"].nunique())
        summary["per_symbol_rows"] = {
            str(r.symbol): int(r.rows) for r in per_symbol.itertuples(index=False)
        }
    return summary


def write_completeness_report(
    report: dict[str, Any], output_dir: Path
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "completeness_report.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    md_path = output_dir / "completeness_report.md"
    md_path.write_text(render_completeness_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_completeness_markdown(report: dict[str, Any]) -> str:
    lines = ["# v0.7 Real-Data Completeness Report", ""]
    lines.append(f"- **As of**: {report['as_of']}")
    lines.append(f"- **Window**: {report['window_start']} → {report['window_end']}")
    lines.append("")
    spec = report["prices"]["source"]
    lines.append("## Price source")
    lines.append(
        f"- `{spec['repo']}` @ `{spec['commit'][:12]}` (data version {spec['data_version']}, last update {spec['last_update']})"
    )
    lines.append(
        f"- Adjustment state: **{spec['adjustment_state']}** — {spec['adjustment_note']}"
    )
    lines.append(f"- License: {spec['license']}")
    per_symbol = report["prices"]["per_symbol"]
    ok = sum(1 for info in per_symbol.values() if info.get("status") == "ok")
    lines.append(f"- Symbols ingested: {ok}/{len(per_symbol)}")
    combined = report["prices"]["combined_quality"]
    lines.append(
        f"- Rows: {combined['total_rows']} total, {combined['accepted_rows']} accepted, "
        f"quality issues: {combined['quality_issue_counts'] or 'none'}"
    )
    adjustment = report.get("adjustment", {})
    lines.append("")
    lines.append("## Adjustment / corporate actions")
    lines.append(
        f"- Adjustment state: **{adjustment.get('state')}** — {adjustment.get('note')}"
    )
    lines.append(
        f"- Window observations on the adjusted series: {adjustment.get('window_observations_adjusted')}"
    )
    lines.append(
        f"- OHLC-inconsistent rows (reported, never repaired): {adjustment.get('ohlc_inconsistent_row_count')} across {len(adjustment.get('ohlc_inconsistent_rows', {}))} symbols"
    )
    lines.append(f"- Window check: {adjustment.get('ohlc_inconsistent_window_check')}")
    lines.append(
        f"- Independent cross-check: {adjustment.get('independent_crosscheck')}"
    )
    lines.append("")
    universe = report["universe"]
    lines.append("## Universe (point-in-time)")
    lines.append(
        f"- Source: `{universe['source_repo']}` @ `{universe['source_commit'][:12]}` — data license **{universe['source_license']}**"
    )
    lines.append(f"- Attribution: {universe['source_attribution']}")
    lines.append(
        f"- Membership rows: {universe['rows']}; symbols ever: {universe['symbols_ever']}; members at as-of: {universe['members_at_as_of_count']}"
    )
    if universe.get("excluded_symbols"):
        lines.append(
            f"- Non-tradeable rows excluded (reported): {', '.join(universe['excluded_symbols'])}"
        )
    lines.append(f"- ISIN coverage: {universe['isin_coverage']}")
    lines.append("")
    panel = report.get("panel")
    if panel:
        lines.append("## Research panel")
        lines.append(
            f"- Trading days: {panel['trading_days']} (market holidays in window: {panel['market_holidays_in_window']})"
        )
        lines.append(
            f"- Complete symbols: {panel['symbols_complete']}; excluded: {panel['symbols_excluded']}"
        )
        for symbol, reason in sorted(panel["excluded_symbols"].items()):
            lines.append(f"  - **{symbol}**: {reason}")
    lines.append("")
    fundamentals = report.get("fundamentals")
    if fundamentals:
        lines.append("## Fundamentals (operator bundle)")
        lines.append(
            f"- Rows: {fundamentals.get('rows', 'pending')}; symbols: {fundamentals.get('symbols', 'pending')}"
        )
    else:
        lines.append("## Fundamentals (operator bundle)")
        lines.append(
            "- PENDING: run `python scripts/ingest_real_data.py --fetch-fundamentals`"
        )
    staleness = report.get("staleness")
    if staleness:
        lines.append("")
        lines.append(f"## Staleness warning: {staleness['detail']}")
    lines.append("")
    lines.append("## Limitations")
    for item in report.get("limitations", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# operator fundamentals fetch (mode B — the single external-data command)
# --------------------------------------------------------------------------


def _next_quarter_end(ts: pd.Timestamp) -> pd.Timestamp:
    """Conservative availability date: a quarter's financials are treated
    as knowable at the *next* quarter end (always after NSE's ~45-day
    filing deadline; v0.7 §13 publication-timing leak control)."""
    next_quarter = ts.to_period("Q") + 1
    return next_quarter.to_timestamp(how="end").normalize()


def fetch_fundamentals(
    output_dir: str | Path,
    *,
    panel_symbols: list[str],
    window_start: str,
    window_end: str,
    clean_dir: Path | None = None,
) -> dict[str, Any]:
    """Fetch quarterly fundamentals via yfinance + an independent yfinance
    price cross-check of the eod2 panel (operator command only).

    Produces (artifacts, no research logic):
      * ``fundamentals_quarterly.parquet`` — long frame
        (date=availability, symbol, roe, debt_to_equity,
        fiscal_quarter_end, source, fetched_at)
      * ``crosscheck_yfinance.json`` — independent raw-close comparison
        of the eod2 panel vs Yahoo (adjustment-quality spot check)
      * ``fundamentals_provenance.json`` — versions, coverage, warnings
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - operator environment
        raise SystemExit(
            "yfinance is required for --fetch-fundamentals (pip install yfinance)"
        ) from exc

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(UTC).isoformat()
    fundamentals_rows: list[dict[str, Any]] = []
    per_symbol_provenance: dict[str, Any] = {}
    crosscheck: dict[str, Any] = {}
    warnings: list[str] = []

    eod2_closes: dict[str, pd.Series] = {}
    if clean_dir is not None:
        for symbol in panel_symbols:
            path = clean_dir / EOD2_SOURCE / f"{symbol}.parquet"
            if path.is_file():
                frame = pd.read_parquet(path)
                frame["date"] = pd.to_datetime(frame["date"])
                in_window = (frame["date"] >= pd.Timestamp(window_start)) & (
                    frame["date"] <= pd.Timestamp(window_end)
                )
                series = frame.loc[in_window].set_index("date")["close"].sort_index()
                eod2_closes[symbol] = series

    import requests

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    for number, symbol in enumerate(sorted(panel_symbols), start=1):
        ticker = symbol + ".NS"
        try:
            handle = yf.Ticker(ticker, session=session)
            history_raw = handle.history(
                start=window_start, end=window_end, auto_adjust=False, actions=False
            )
            income = handle.quarterly_income_stmt
            balance = handle.quarterly_balance_sheet
        except Exception as exc:  # network hiccups: record, continue
            per_symbol_provenance[symbol] = {
                "status": "fetch_failed",
                "error": str(exc),
            }
            warnings.append(f"{symbol}: fetch failed ({exc})")
            continue

        # ---- independent price cross-check (eod2 split/bonus-adjusted ----
        # closes vs yfinance raw, unadjusted closes): days where the two
        # diverge materially are candidate split/bonus dates (dividends
        # leave both series untouched, so they do not register here).
        if symbol in eod2_closes and not history_raw.empty:
            yahoo_close = history_raw["Close"].astype(float)
            yahoo_close.index = yahoo_close.index.tz_localize(None)
            joined = pd.concat(
                [eod2_closes[symbol], yahoo_close], axis=1, keys=("eod2", "yfinance")
            ).dropna()
            if len(joined) >= 10:
                diff_pct = (
                    (joined["eod2"] - joined["yfinance"]).abs() / joined["eod2"] * 100.0
                )
                mismatches = joined.index[diff_pct > 1.5]
                crosscheck[symbol] = {
                    "days_compared": int(len(joined)),
                    "mean_abs_diff_pct": round(float(diff_pct.mean()), 4),
                    "max_abs_diff_pct": round(float(diff_pct.max()), 4),
                    "p99_abs_diff_pct": round(float(diff_pct.quantile(0.99)), 4),
                    "mismatches_gt_1_5pct": [str(d.date()) for d in mismatches[:10]],
                    "n_mismatches_gt_1_5pct": int(len(mismatches)),
                }
            else:
                crosscheck[symbol] = {"days_compared": int(len(joined))}

        # ---- quarterly fundamentals (ROE, debt/equity) -------------------
        symbol_rows: list[dict[str, Any]] = []
        quarter_notes: list[str] = []
        if (
            income is not None
            and not income.empty
            and balance is not None
            and not balance.empty
        ):
            columns = sorted(
                set(income.columns) & set(balance.columns), key=lambda c: c
            )
            equity = (
                balance.loc["Total Stockholders Equity"]
                if "Total Stockholders Equity" in balance.index
                else (
                    balance.loc["Stockholders Equity"]
                    if "Stockholders Equity" in balance.index
                    else None
                )
            )
            total_debt = (
                balance.loc["Total Debt"] if "Total Debt" in balance.index else None
            )
            net_income = (
                income.loc["Net Income"] if "Net Income" in income.index else None
            )
            if equity is None or total_debt is None or net_income is None:
                per_symbol_provenance[symbol] = {
                    "status": "missing_statements",
                    "note": "quarterly statements incomplete",
                }
                warnings.append(f"{symbol}: quarterly statements incomplete")
                continue
            previous_equity: float | None = None
            for column in columns:
                quarter_end = pd.Timestamp(column)
                if quarter_end.date() < pd.Timestamp(window_start).date():
                    previous_equity = (
                        float(equity[column])
                        if pd.notna(equity[column])
                        else previous_equity
                    )
                    continue
                if quarter_end > pd.Timestamp(window_end):
                    continue
                ni = net_income[column]
                eq = equity[column]
                debt = total_debt[column]
                if pd.isna(eq) or eq <= 0:
                    quarter_notes.append(f"{quarter_end.date()}: non-positive equity")
                    previous_equity = None
                    continue
                roe = None
                if pd.notna(ni):
                    average_equity = (
                        (float(eq) + float(previous_equity)) / 2.0
                        if previous_equity is not None and previous_equity > 0
                        else float(eq)
                    )
                    if average_equity > 0:
                        roe = float(ni) / average_equity
                debt_to_equity = float(debt) / float(eq) if pd.notna(debt) else None
                previous_equity = float(eq)
                if roe is None and debt_to_equity is None:
                    quarter_notes.append(f"{quarter_end.date()}: no usable figures")
                    continue
                symbol_rows.append(
                    {
                        # Conservative availability: next quarter end.
                        "date": _next_quarter_end(quarter_end),
                        "symbol": symbol,
                        "roe": roe,
                        "debt_to_equity": debt_to_equity,
                        "fiscal_quarter_end": quarter_end.date().isoformat(),
                        "source": "yfinance",
                        "fetched_at": fetched_at,
                    }
                )
        else:
            per_symbol_provenance[symbol] = {"status": "no_statements"}
            warnings.append(f"{symbol}: no quarterly statements available")
            continue

        fundamentals_rows.extend(symbol_rows)
        per_symbol_provenance[symbol] = {
            "status": "ok",
            "ticker": ticker,
            "quarters": len(symbol_rows),
            "notes": quarter_notes,
        }
        if number % 20 == 0:
            print(f"  fetched {number}/{len(panel_symbols)} symbols ...", flush=True)
        time.sleep(0.3)  # polite pacing for the public Yahoo endpoint

    if not fundamentals_rows:
        raise SystemExit(
            "fundamentals fetch produced no rows; check network access to "
            "Yahoo Finance and retry"
        )
    bundle = pd.DataFrame(fundamentals_rows)
    for column in FUNDAMENTALS_COLUMNS:
        if column not in bundle.columns:
            bundle[column] = None
    bundle = bundle.sort_values(["symbol", "date"]).reset_index(drop=True)
    parquet_path = out / "fundamentals_quarterly.parquet"
    bundle.to_parquet(parquet_path, index=False)
    (out / "crosscheck_yfinance.json").write_text(
        json.dumps(crosscheck, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fingerprint = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    provenance = {
        "fetched_at": fetched_at,
        "yfinance_version": getattr(yf, "__version__", "unknown"),
        "pandas_version": pd.__version__,
        "symbols_requested": len(panel_symbols),
        "symbols_ok": sum(
            1 for info in per_symbol_provenance.values() if info.get("status") == "ok"
        ),
        "rows": int(len(bundle)),
        "bundle_fingerprint": fingerprint,
        "availability_rule": (
            "a fiscal quarter's figures are treated as available at the "
            "NEXT quarter end (conservative vs NSE's ~45-day filing "
            "deadline; no publication look-ahead)"
        ),
        "roe_definition": (
            "quarter net income / average(total stockholders equity at "
            "quarter end and prior quarter end); cross-sectional ranking "
            "only, so scale is immaterial"
        ),
        "debt_to_equity_definition": "total debt / total stockholders equity at quarter end",
        "crosscheck_note": (
            "independent close comparison of the eod2 panel "
            "(split/bonus-adjusted) vs yfinance raw (unadjusted) closes; "
            "mismatches > 1.5% list candidate split/bonus dates for "
            "manual review (dividends leave both series untouched, so "
            "they are not detected here; report only — never "
            "auto-repaired)"
        ),
        "per_symbol": per_symbol_provenance,
        "warnings": warnings,
    }
    (out / "fundamentals_provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "bundle": str(parquet_path),
                "rows": provenance["rows"],
                "symbols_ok": provenance["symbols_ok"],
                "warnings": len(warnings),
            },
            indent=2,
        )
    )
    return provenance


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _panels_and_bundle_context(
    as_of: str,
    window_start: str,
    universe_root: Path,
) -> tuple[realdata.ResearchPanels, list[str], Path]:
    """Load the validated clean layer + PIT universe; build the panels."""
    catalog = CleanDataCatalog()
    if not (universe_root / "nifty100-pit" / "nifty100.csv").is_file():
        raise SystemExit(
            "point-in-time universe missing — run the local ingestion first: "
            "python scripts/ingest_real_data.py --local ..."
        )
    symbols = requested_constituents(
        universe_root, window_start=window_start, as_of=as_of
    )
    panels = realdata.build_market_panels(
        catalog,
        symbols,
        source=EOD2_SOURCE,
        window_start=window_start,
        window_end=as_of,
    )
    return panels, symbols, universe_root


def _read_symbol_list(path: Path) -> list[str]:
    return [
        line.strip().upper()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _panel_symbols_for_fetch(
    as_of: str,
    window_start: str,
    panel_symbols_file: Path | None,
    universe_root: Path,
) -> list[str]:
    """Resolve the research panel symbols for the operator fetch.

    Precedence: explicit ``--panel-symbols-file`` > the milestone
    ``<universe_root>/panel_symbols.txt`` (keeps a fresh machine
    self-contained) > derivation from the local clean layer (this
    workspace, where ingestion already ran).
    """
    if panel_symbols_file is not None:
        if not panel_symbols_file.is_file():
            raise SystemExit(f"panel symbols file missing: {panel_symbols_file}")
        return _read_symbol_list(panel_symbols_file)
    committed = universe_root / "panel_symbols.txt"
    if committed.is_file():
        return _read_symbol_list(committed)
    panels, _, _ = _panels_and_bundle_context(as_of, window_start, universe_root)
    return list(panels.symbols)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--local",
        action="store_true",
        help="offline ingestion of the pinned eod2 + membership checkouts",
    )
    mode.add_argument(
        "--fetch-fundamentals",
        action="store_true",
        help="OPERATOR command: fetch quarterly fundamentals via yfinance",
    )
    mode.add_argument(
        "--from-bundle",
        type=Path,
        help="merge an operator fundamentals bundle into the research dataset",
    )
    parser.add_argument("--eod2-dir", type=Path, default=None)
    parser.add_argument("--membership-dir", type=Path, default=None)
    parser.add_argument(
        "--universe-root",
        type=Path,
        default=ROOT / "data" / "universe",
        help="where the point-in-time universe CSVs + provenance are written",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "reports" / "generated" / "real_data",
        help="where the completeness report is written",
    )
    parser.add_argument("--as-of", default=DEFAULT_AS_OF)
    parser.add_argument("--window-start", default=DEFAULT_WINDOW_START)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "bundle",
        help="operator bundle output directory (default data/bundle)",
    )
    parser.add_argument(
        "--panel-symbols-file",
        type=Path,
        default=None,
        help="explicit symbol list for --fetch-fundamentals (default: derived)",
    )
    args = parser.parse_args(argv)

    output_root = args.report_dir
    universe_root = args.universe_root

    if args.local:
        if args.eod2_dir is None or args.membership_dir is None:
            raise SystemExit("--local requires --eod2-dir and --membership-dir")
        eod2_dir = args.eod2_dir.expanduser()
        membership_dir = args.membership_dir.expanduser()
        retrieved_at = datetime.now(UTC).isoformat()
        storage = StorageManager()

        # membership first (defines the requested constituent set)
        isin_map = load_isin_map(eod2_dir)
        membership_audit = ingest_membership(
            membership_dir,
            isin_map=isin_map,
            as_of=args.as_of,
            retrieved_at=retrieved_at,
            universe_root=universe_root,
        )
        symbols = requested_constituents(
            universe_root, window_start=args.window_start, as_of=args.as_of
        )
        # Parse once; the data-derived market calendar (holidays + special
        # sessions included) drives the clean layer's off-calendar checks.
        pre_parsed: dict[str, pd.DataFrame] = {}
        spec_for_parse = Eod2SourceSpec.from_meta(
            load_meta_json(eod2_dir), commit=_git_head(eod2_dir)
        )
        for symbol in symbols:
            path = eod2_dir / "daily" / symbol_to_filename(symbol)
            if path.is_file():
                pre_parsed[symbol] = parse_eod2_daily_file(
                    path, symbol, spec=spec_for_parse
                )
        windowed_frames = {
            symbol: _window_rows(frame, args.window_start, args.as_of)
            for symbol, frame in pre_parsed.items()
        }
        derived_calendar = realdata.market_calendar(
            windowed_frames, start=args.window_start, end=args.as_of
        )
        catalog = CleanDataCatalog(calendar=derived_calendar)
        price_audit = ingest_prices(
            pre_parsed,
            eod2_dir=eod2_dir,
            symbols=symbols,
            as_of=args.as_of,
            window_start=args.window_start,
            window_end=args.as_of,
            catalog=catalog,
            storage=storage,
        )

        panels, _, _ = _panels_and_bundle_context(
            args.as_of, args.window_start, universe_root
        )
        staleness = detect_data_staleness(
            panels.close.index,
            reference_now=datetime.now(UTC),
            max_staleness_days=6.0,
        )
        report = build_completeness_report(
            panels=panels,
            price_audit=price_audit,
            membership_audit={
                **membership_audit,
                "source_repo": "aditya-jha/nse-historical-membership",
                "source_commit": _git_head(membership_dir),
                "source_license": "CC BY 4.0",
                "source_attribution": (
                    "point-in-time NSE index membership from "
                    "github.com/aditya-jha/nse-historical-membership "
                    "(data: CC BY 4.0); underlying source: NSE index press "
                    "releases / exchange circulars (publicly published)"
                ),
            },
            as_of=args.as_of,
            window_start=args.window_start,
            staleness_issue=staleness,
        )
        json_path, md_path = write_completeness_report(report, output_root)
        # Committed derived artifact: the exact research panel symbol list
        # (keeps the operator fundamentals command self-contained on a
        # fresh machine; see docs/real_data.md).
        panel_symbols_path = universe_root / "panel_symbols.txt"
        panel_symbols_path.write_text(
            "# v0.7 research panel symbols (complete gap-free price history\n"
            "# in window; derived deterministically — see completeness report)\n"
            + "\n".join(panels.symbols)
            + "\n",
            encoding="utf-8",
        )
        latest = panels.close.index.max()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "panel_symbols": len(panels.symbols),
                    "panel_symbols_file": str(panel_symbols_path),
                    "excluded": report["panel"]["excluded_symbols"],
                    "trading_days": panels.window.trading_days,
                    "latest_observation": str(latest.date()),
                    "completeness_report": [str(json_path), str(md_path)],
                },
                indent=2,
                default=str,
            )
        )
        return 0

    if args.fetch_fundamentals:
        symbols = _panel_symbols_for_fetch(
            args.as_of, args.window_start, args.panel_symbols_file, universe_root
        )
        catalog = CleanDataCatalog()
        fetch_fundamentals(
            args.output,
            panel_symbols=symbols,
            window_start=args.window_start,
            window_end=args.as_of,
            clean_dir=catalog.clean_dir,
        )
        return 0

    # --from-bundle <dir>
    bundle = args.from_bundle
    if bundle is None:
        raise SystemExit("--from-bundle requires a directory")
    bundle = bundle.expanduser()
    if not bundle.is_dir():
        raise SystemExit(f"bundle directory missing: {bundle}")
    frame, provenance = realdata.load_fundamentals_bundle(bundle, as_of=args.as_of)
    panels, _, _ = _panels_and_bundle_context(
        args.as_of, args.window_start, universe_root
    )
    missing = set(panels.symbols) - set(frame["symbol"])
    extra = set(frame["symbol"]) - set(panels.symbols)
    report = build_completeness_report(
        panels=panels,
        price_audit=_read_price_audit(output_root)
        or {
            "source_spec": {},
            "per_symbol": {},
            "combined": {
                "total_rows": 0,
                "accepted_rows": 0,
                "quality_issue_counts": {},
            },
        },
        membership_audit=_read_universe_audit(output_root)
        or {
            "rows": 0,
            "symbols_ever": 0,
            "members_at_as_of_count": 0,
            "excluded_symbols": [],
            "isin_coverage": {},
        },
        as_of=args.as_of,
        window_start=args.window_start,
        bundle_dir=bundle,
    )
    report["fundamentals"]["panel_symbols_missing"] = sorted(missing)
    report["fundamentals"]["extra_symbols"] = sorted(extra)
    report["fundamentals"]["availability_rule"] = provenance.get("availability_rule")
    json_path, md_path = write_completeness_report(report, output_root)
    print(
        json.dumps(
            {
                "status": "ok",
                "fundamentals_rows": int(len(frame)),
                "fundamentals_symbols": int(frame["symbol"].nunique()),
                "panel_symbols_missing": sorted(missing),
                "completeness_report": [str(json_path), str(md_path)],
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _read_price_audit(output_root: Path) -> dict[str, Any] | None:
    path = output_root / "completeness_report.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "source_spec": payload.get("prices", {}).get("source", {}),
        "per_symbol": payload.get("prices", {}).get("per_symbol", {}),
        "combined": payload.get("prices", {}).get("combined", {}),
        "ohlc_inconsistencies": payload.get("adjustment", {}).get(
            "ohlc_inconsistent_rows", {}
        ),
    }


def _read_universe_audit(output_root: Path) -> dict[str, Any] | None:
    path = output_root / "completeness_report.json"
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("universe")


if __name__ == "__main__":
    raise SystemExit(main())
