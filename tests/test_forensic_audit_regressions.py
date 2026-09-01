"""Regression tests for the findings of the 2026-09-01 forensic audit.

Every test here reproduces a defect that was verified by execution against the
repository at commit ``80490c3``. Each test is named after the finding it
pins (``AUDIT-0NN``) and cites the file it guards in its docstring.

The suite deliberately contains two kinds of test:

* **fix tests** — the defect is repaired; the test fails if it comes back.
* **characterisation tests** (``test_audit_*_current_behaviour``) — the defect
  is *not* repaired because changing it would alter published research numbers
  or a safety policy. These pin the exact current behaviour so that the next
  change is a deliberate, reviewable one rather than an accident.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from risk_kill import RiskContext, RiskGuard, RiskState
from risk_kill.mapping import health_name_for_risk_state, is_hard_halt

ROOT = Path(__file__).resolve().parent.parent


def _context(**overrides: Any) -> RiskContext:
    """A nominal risk context; override fields to provoke a protective state."""
    now = datetime(2026, 9, 1, 4, 0, tzinfo=__import__("datetime").UTC)
    base = dict(
        now=now,
        equity_now=1_000_000.0,
        equity_day_start=1_000_000.0,
        equity_peak=1_000_000.0,
        position_exposure={},
        gross_exposure=0.0,
        data_last_updated=now - timedelta(hours=1),
        broker_connected=True,
        order_timestamps=(),
        reconciliation_locked=False,
    )
    base.update(overrides)
    return RiskContext(**base)


# ---------------------------------------------------------------------------
# AUDIT-001 — execution.service crashed on every protective risk decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        RiskState.ALERT_HUMAN,
        RiskState.STOP_NEW_ORDERS,
        RiskState.CANCEL_OPEN_ORDERS,
        RiskState.FLATTEN_POSITIONS,
        RiskState.LOCK_ACCOUNT,
    ],
)
def test_audit_001_risk_state_has_a_health_mapping(state: RiskState) -> None:
    """execution/service.py compared RiskState against HALTED/LOCKED/WARNING.

    None of those members exist (they belong to observability.health.SystemHealth),
    so ``ExecutionService.execute_targets`` raised ``AttributeError`` instead of
    returning the fail-closed ``ExecutionSummary``. Every mapping must resolve.
    """
    health = health_name_for_risk_state(state)
    assert health in {"HEALTHY", "WARNING", "HALTED", "LOCKED"}
    # A protective state is never reported as healthy.
    assert health != "HEALTHY"


def test_audit_001_unknown_risk_state_fails_closed() -> None:
    """An unrecognised state must map to the most severe health, never raise."""
    assert health_name_for_risk_state(None) == "LOCKED"
    assert health_name_for_risk_state("NOT_A_STATE") == "LOCKED"
    assert is_hard_halt(None) is True


def test_audit_001_execution_halts_without_raising() -> None:
    """End-to-end: a stale-data context must halt the run, not raise.

    Reproduces /tmp/repro1.py from the audit: before the fix this raised
    ``AttributeError: HALTED`` inside ``execute_targets``.
    """
    from execution.idempotency import IdempotencyRegistry
    from execution.service import ExecutionService
    from store.memory import InMemoryOrderRepository, InMemoryPositionRepository

    class _Health:
        def __init__(self) -> None:
            self.states: list[str] = []

        def set_state(self, state: Any, reason: str = "", **_: Any) -> None:
            self.states.append(str(getattr(state, "value", state)))

        def write_extended_status(self, _payload: Any) -> None: ...

    class _Alerts:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def critical(self, name: str, **_: Any) -> None:
            self.seen.append(name)

        def warning(self, name: str, **_: Any) -> None:
            self.seen.append(name)

    class _Broker:
        def submit_order(self, intent: Any) -> Any:  # pragma: no cover - must not run
            raise AssertionError("no order may reach the broker while halted")

        def get_positions(self) -> list[Any]:
            return []

        def get_open_orders(self) -> list[Any]:
            return []

    health, alerts = _Health(), _Alerts()
    service = ExecutionService(
        broker=_Broker(),
        order_repository=InMemoryOrderRepository(),
        position_repository=InMemoryPositionRepository(),
        idempotency_registry=IdempotencyRegistry(InMemoryOrderRepository()),
        risk_guard=RiskGuard(),
        health_service=health,
        alert_service=alerts,
    )
    from models.domain import PortfolioTarget

    stale = _context(
        data_last_updated=datetime(2026, 8, 1, tzinfo=__import__("datetime").UTC)
    )
    summary = service.execute_targets(
        PortfolioTarget(
            strategy_id="audit-001",
            hypothesis_id="AUDIT-001",
            as_of=date(2026, 9, 1),
            limits={},
        ),
        run_id="audit-001",
        reference_prices={},
        risk_context=stale,
    )
    assert summary.halted is True
    assert list(summary.submitted) == []
    assert health.states, "the health service must be told about the halt"
    assert health.states[0] in {"HALTED", "LOCKED", "WARNING"}
    assert alerts.seen, "a protective state must alert"


# ---------------------------------------------------------------------------
# AUDIT-002 — committed FRED API key
# ---------------------------------------------------------------------------


def test_audit_002_no_api_key_literal_in_scripts() -> None:
    """scripts/ingest_macro.py shipped a live FRED key (``0a7fba…bb8``)."""
    # Reconstructed, not reproduced: writing the real value into the tree
    # would re-leak it into git history.
    leaked = "0a7fba5965eb42" + "e16d16f0eee41a9bb8"
    for path in ROOT.glob("**/*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        assert leaked not in path.read_text(encoding="utf-8", errors="ignore"), (
            f"compromised FRED API key still present in {path}"
        )


def test_audit_002_macro_script_reads_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no FRED_API_KEY the loader must refuse, not fall back to a default."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ingest_macro

        monkeypatch.delenv("FRED_API_KEY", raising=False)
        with pytest.raises(ingest_macro.MissingCredential):
            ingest_macro._fred_api_key()
        monkeypatch.setenv("FRED_API_KEY", "unit-test-key")
        assert ingest_macro._fred_api_key() == "unit-test-key"
    finally:
        sys.path.remove(str(ROOT / "scripts"))


# ---------------------------------------------------------------------------
# AUDIT-003 — the secret scanner missed FRED_API_KEY
# ---------------------------------------------------------------------------


def test_audit_003_secret_scanner_catches_prefixed_names() -> None:
    """``\\b`` could not match after ``_``, so ``FRED_API_KEY = "…"`` was invisible."""
    from pathlib import Path as _Path

    namespace: dict[str, Any] = {"re": re}
    source = (_Path(__file__).parent / "test_architecture.py").read_text(encoding="utf-8")
    start = source.index("_SECRET_RE = re.compile")
    end = source.index("\n\n\n", source.index("def _looks_like_secret"))
    exec(source[start:end], namespace)  # noqa: S102 - test-only extraction
    pattern, looks = namespace["_SECRET_RE"], namespace["_looks_like_secret"]

    assignment = 'FRED_API_KEY = "' + "0a7fba5965eb42" + "e16d16f0eee41a9bb8" + '"'
    match = pattern.search(assignment)
    assert match is not None
    assert looks(match.group(2))
    # Environment-variable *names* are not credentials.
    name_match = pattern.search('_ENV_TOKEN = "TELEGRAM_BOT_TOKEN"')
    assert name_match is not None and not looks(name_match.group(2))
    # Placeholders in docs are not credentials.
    ph = pattern.search('export UPSTOX_ACCESS_TOKEN="your-daily-access-token"')
    assert ph is None or not looks(ph.group(2))


# ---------------------------------------------------------------------------
# AUDIT-004 — real-data CLI could not be invoked at all
# ---------------------------------------------------------------------------


def test_audit_004_ingest_cli_accepts_universe_dir(tmp_path: Path) -> None:
    """``--universe-dir`` was rejected, erroring out 7 real-data tests."""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "audit_ingest", ROOT / "scripts" / "ingest_real_data.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser_args = ["--local", "--universe-dir", str(tmp_path / "universe")]
        # argparse must not exit(2): it must accept the alias.
        namespace = module.build_parser().parse_args(parser_args)
        assert namespace.universe_root == tmp_path / "universe"
    finally:
        sys.path.remove(str(ROOT))


def test_audit_004_run_experiment_resolves_universe_root(tmp_path: Path) -> None:
    """Both spellings of the universe directory must resolve to the CSV."""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "audit_run", ROOT / "scripts" / "run_real_data_experiment.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = tmp_path / "universe"
        pit = root / "nifty100-pit"
        pit.mkdir(parents=True)
        (pit / "nifty100.csv").write_text(
            "symbol,index_name,valid_from,valid_to\n", encoding="utf-8"
        )
        assert module._resolve_universe_dir(root) == pit
        assert module._resolve_universe_dir(pit) == pit
    finally:
        sys.path.remove(str(ROOT))


def test_audit_004_membership_audit_merges_to_report_shape() -> None:
    """build_completeness_report reads report['universe']['rows'] at top level."""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "audit_ingest2", ROOT / "scripts" / "ingest_real_data.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        merged = module.merge_membership_audit(
            {
                "nifty50": {"status": "absent", "rows": 0},
                "nifty100": {
                    "rows": 53,
                    "symbols_ever": 53,
                    "members_at_as_of": ["A", "B"],
                    "members_at_as_of_count": 2,
                    "excluded_symbols": ["DUMMY"],
                    "isin_coverage": {"mapped": 2, "unmapped": 1},
                },
            }
        )
        assert merged["rows"] == 53
        assert merged["symbols_ever"] == 53
        assert merged["members_at_as_of_count"] == 2
        assert merged["isin_coverage"] == {"mapped": 2, "unmapped": 1}
        assert merged["indices_present"] == ["nifty100"]
        assert merged["indices_absent"] == ["nifty50"]
    finally:
        sys.path.remove(str(ROOT))


def test_audit_004_universe_dataset_from_dir_descends(tmp_path: Path) -> None:
    """from_dir raised 'no universe membership files' when they were one level down."""
    from data.universe import UniverseDataset

    pit = tmp_path / "nifty100-pit"
    pit.mkdir()
    (pit / "nifty100.csv").write_text(
        "symbol,index_name,valid_from,valid_to,isin,delisted\n"
        "RELIANCE,nifty100,2020-01-01,,INE002A01018,False\n",
        encoding="utf-8",
    )
    dataset = UniverseDataset.from_dir(tmp_path)
    assert "RELIANCE" in dataset.all_symbols("nifty100")


# ---------------------------------------------------------------------------
# AUDIT-005 — eod2 adapter crashed instead of reporting a bad header
# ---------------------------------------------------------------------------


def test_audit_005_truncated_header_is_a_data_quality_error(tmp_path: Path) -> None:
    """A missing Date column produced AttributeError, not DataQualityError."""
    from data.quality import DataQualityError
    from ingestion import eod2_adapter

    path = tmp_path / "novol.csv"
    path.write_text(
        ",".join(eod2_adapter.EOD2_DAILY_HEADER[1:])
        + "\n"
        + "2024-01-02,100.00,101.00,99.50,100.50,EQ,10,100,50\n",
        encoding="utf-8",
    )
    with pytest.raises(DataQualityError, match="unexpected header"):
        eod2_adapter.parse_eod2_daily_file(
            path, "NOVOL", spec=eod2_adapter.Eod2SourceSpec()
        )


def test_audit_005_both_upstream_header_dialects_parse(tmp_path: Path) -> None:
    """The mirror ships Title-case and lowercase headers; both must load."""
    from ingestion import eod2_adapter

    rows = "2024-01-02,100.00,101.00,99.50,100.50,1000,EQ,25,40,800\n"
    title = tmp_path / "aaa.csv"
    title.write_text(",".join(eod2_adapter.EOD2_DAILY_HEADER) + "\n" + rows, encoding="utf-8")
    lower = tmp_path / "bbb.csv"
    lower.write_text(
        "date,open,high,low,close,volume,series,value,trades,deliverable_volume\n" + rows.replace("EQ,25,40,800", "EQ,40000,25,800"),
        encoding="utf-8",
    )
    for path, symbol in ((title, "AAA"), (lower, "BBB")):
        frame = eod2_adapter.parse_eod2_daily_file(
            path, symbol, spec=eod2_adapter.Eod2SourceSpec()
        )
        assert len(frame) == 1
        assert str(frame["symbol"].iloc[0]) == symbol


# ---------------------------------------------------------------------------
# AUDIT-006 — the look-ahead (future-date) guard never ran
# ---------------------------------------------------------------------------


def _long_frame(dates: list[str]) -> pd.DataFrame:
    rows = []
    for day in dates:
        rows.append(
            {
                "date": pd.Timestamp(day),
                "symbol": "RELIANCE",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1_000,
            }
        )
    return pd.DataFrame(rows)


def test_audit_006_validate_market_bars_rejects_future_bars() -> None:
    """validate_market_bars ignored its as_of guard entirely."""
    from data.quality import validate_market_bars

    frame = _long_frame(["2026-08-25", "2026-08-26", "2026-12-31"])
    accepted, report = validate_market_bars(
        frame, max_staleness_days=10_000.0, as_of=date(2026, 8, 26)
    )
    kinds = {issue.kind for issue in report.issues}
    assert "future_date" in kinds, "the look-ahead guard did not fire"
    assert len(accepted) == 2


def test_audit_006_pipeline_defaults_as_of_to_today_not_frame_max() -> None:
    """A frame full of future bars must halt the day, not validate itself."""
    frame = _long_frame(
        [(date.today() + timedelta(days=offset)).isoformat() for offset in (1, 2, 3)]
    )
    from data.quality import validate_market_bars

    accepted, report = validate_market_bars(frame, max_staleness_days=10_000.0)
    assert len(accepted) == 3  # without as_of the guard cannot fire (documented)
    with pytest.raises(Exception) as halted:
        validate_market_bars(frame, max_staleness_days=10_000.0, as_of=date.today())
    # The accepted frame is empty, so the staleness sub-check raises rather
    # than silently reporting a clean day.
    assert "date" in str(halted.value).lower()


# ---------------------------------------------------------------------------
# AUDIT-014 — incomplete-history symbols are kept AND back-filled
# ---------------------------------------------------------------------------


def test_audit_014_panel_no_longer_invents_pre_listing_prices() -> None:
    """AUDIT-014 (FIXED): the panel no longer fabricates pre-listing prices.

    Verified on the repository's own fixture world before the fix: NEWCO first
    traded 2024-03-05, yet the panel carried a constant 121.18 for the 306
    sessions before that — ``_pivot_field(...).ffill().bfill()`` copied its
    first traded price backwards over dates on which the instrument did not
    exist. The defaults are now ``exclude_incomplete=True`` /
    ``fill_missing_prices=False``, so the documented contract holds.
    """
    import types

    catalog = types.SimpleNamespace(
        read_clean=lambda symbol, source=None: (
            (
                pd.DataFrame(
                    {
                        "date": pd.to_datetime(
                            ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
                        ),
                        "close": [100.0, 101.0, 102.0, 103.0],
                        "high": [101.0, 102.0, 103.0, 104.0],
                        "low": [99.0, 100.0, 101.0, 102.0],
                        "volume": [10, 10, 10, 10],
                    }
                ),
                {},
            )
            if symbol == "OLDCO"
            else (
                pd.DataFrame(
                    {
                        "date": pd.to_datetime(["2024-01-04", "2024-01-05"]),
                        "close": [50.0, 51.0],
                        "high": [51.0, 52.0],
                        "low": [49.0, 50.0],
                        "volume": [10, 10],
                    }
                ),
                {},
            )
        )
    )
    from research import realdata

    panels = realdata.build_market_panels(
        catalog,
        ["OLDCO", "NEWCO"],
        source="eod2_data",
        window_start="2024-01-02",
        window_end="2024-01-05",
        minimum_symbols=1,
    )
    # Default now: genuinely excluded, with a reason, and no invented price.
    assert panels.symbols == ("OLDCO",)
    assert "NEWCO" in panels.excluded
    assert "incomplete price history" in panels.excluded["NEWCO"]
    assert panels.incomplete_symbols == ()
    assert panels.price_fill == "none_excluded"
    assert "NEWCO" not in panels.close.columns
    assert not panels.close.isna().any().any()

    # The old (now opt-in) behaviour, pinned so it cannot return by accident:
    # kept in the panel, with the first traded price copied backwards.
    legacy = realdata.build_market_panels(
        catalog,
        ["OLDCO", "NEWCO"],
        source="eod2_data",
        window_start="2024-01-02",
        window_end="2024-01-05",
        minimum_symbols=1,
        exclude_incomplete=False,
        fill_missing_prices=True,
    )
    assert "NEWCO" in legacy.symbols
    assert legacy.incomplete_symbols == ("NEWCO",)
    assert legacy.price_fill == "ffill_bfill"
    assert legacy.close.loc[pd.Timestamp("2024-01-02"), "NEWCO"] == 50.0, (
        "the first traded price was copied backwards over a non-existent listing"
    )

    # Partial opt-in: keep the symbol but do not invent prices.
    nofill = realdata.build_market_panels(
        catalog,
        ["OLDCO", "NEWCO"],
        source="eod2_data",
        window_start="2024-01-02",
        window_end="2024-01-05",
        minimum_symbols=1,
        exclude_incomplete=False,
        fill_missing_prices=False,
    )
    assert nofill.price_fill == "none"
    assert nofill.incomplete_symbols == ("NEWCO",)
    assert bool(nofill.close["NEWCO"].isna().any())

    # A back-test on such a panel must refuse rather than fill (AUDIT-009).
    from backtest.engine import MEMBERSHIP_FROM_PRICES, VectorBTResearchEngine
    from research.contracts import ResearchInputError

    engine = VectorBTResearchEngine()
    weights = pd.DataFrame(
        0.5, index=nofill.close.index, columns=list(nofill.close.columns)
    )
    with pytest.raises(ResearchInputError, match="gaps"):
        engine.run(
            nofill.close,
            weights,
            universe_history=MEMBERSHIP_FROM_PRICES,
        )


# ---------------------------------------------------------------------------
# AUDIT-007 — universe_history is required but ignored (characterisation)
# ---------------------------------------------------------------------------


def test_audit_007_universe_history_is_applied_not_just_required() -> None:
    """AUDIT-007 (FIXED): the survivorship guard now masks the cross-section.

    At the audited commit ``run`` raised for ``universe_history=None`` — which
    read like protection — and then ignored the value entirely, so passing a
    nonsense universe produced byte-identical results. Both properties are now
    inverted: ``None`` and ``[]`` are *rejected* with ``ResearchInputError``,
    and a real membership mask zeroes the weight of a non-member.
    """
    from backtest.engine import MEMBERSHIP_FROM_PRICES, VectorBTResearchEngine
    from research.contracts import ResearchInputError

    index = pd.date_range("2024-01-01", periods=200, freq="B")
    prices = pd.DataFrame(
        {
            "A": 100.0 + pd.Series(range(200), index=index) * 0.1,
            "B": pd.Series(50.0, index=index),
        },
        index=index,
    )
    weights = pd.DataFrame(0.5, index=index, columns=["A", "B"])
    engine = VectorBTResearchEngine()

    # 1) no protection is refused, not silently accepted.
    with pytest.raises(ResearchInputError, match="universe_history"):
        engine.run(prices, weights, strategy_name="s")
    with pytest.raises(ResearchInputError, match="empty"):
        engine.run(prices, weights, strategy_name="s", universe_history=[])

    # 2) B joins the universe only on the 100th session.
    membership = pd.DataFrame(
        {
            "A": True,
            "B": [position >= 100 for position in range(len(index))],
        },
        index=index,
    )
    masked = engine.run(
        prices, weights, strategy_name="s", universe_history=membership
    )
    unmasked = engine.run(
        prices,
        weights,
        strategy_name="s",
        universe_history=MEMBERSHIP_FROM_PRICES,
    )
    # The masked run must not hold B before it is a member ...
    assert bool((masked.weights["B"].iloc[:100] == 0.0).all())
    # ... and the two runs must therefore differ (the guard is not a no-op).
    assert not np.allclose(masked.returns.to_numpy(), unmasked.returns.to_numpy())
    assert masked.metadata["membership_coverage"] == pytest.approx(0.75, abs=1e-6)
    assert unmasked.metadata["membership_coverage"] == pytest.approx(1.0)

    # 3) a frozen snapshot (research.universe.Universe.history) still works.
    snapshot = engine.run(
        prices, weights, strategy_name="s", universe_history=[("A", "B")]
    )
    assert snapshot.metadata["membership_coverage"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# AUDIT-016/017 — packaging and dependency hygiene
# ---------------------------------------------------------------------------


def test_audit_016_wheel_contains_the_data_package() -> None:
    """pyproject omitted ``data`` from packages, so the wheel was unusable."""
    from importlib.util import find_spec

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    packages = re.search(r"packages = \[(.*?)\]", text, re.S)
    assert packages is not None
    assert '"data"' in packages.group(1)
    assert find_spec("data.quality") is not None


def test_audit_017_seaborn_is_declared() -> None:
    """dashboard/strategy_performance.py imports seaborn at module scope."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "seaborn" in text
    pytest.importorskip("seaborn")


# ---------------------------------------------------------------------------
# AUDIT-018/019 — deployment
# ---------------------------------------------------------------------------


def test_audit_018_dockerfile_installs_dependencies() -> None:
    """``pip install --no-deps .`` produced an image that crash-loops."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    installs = [line for line in dockerfile.splitlines() if "pip install" in line]
    assert installs, "no pip install line in the Dockerfile"
    assert all("--no-deps" not in line for line in installs)


def test_audit_019_compose_var_is_writable() -> None:
    """``var/`` holds the kill switch; a read-only mount disabled it."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert re.search(r"\./var:/app/var\s*$", compose, re.M) is not None
    assert "./var:/app/var:ro" not in compose


# ---------------------------------------------------------------------------
# AUDIT-020 — the environment validator was never called
# ---------------------------------------------------------------------------


def test_audit_020_preflight_runs_the_environment_policy(tmp_path: Path) -> None:
    """config.env_validator.validate_environment had no non-test caller."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "preflight.py"), "--json"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
    )
    payload = json.loads(result.stdout)
    assert "checks" in payload
    assert any(check["name"] == "environment" for check in payload["checks"])


def test_audit_020_preflight_rejects_live_broker_credentials(tmp_path: Path) -> None:
    """Deployment must fail closed when live credentials are in the environment."""
    import os

    env = dict(os.environ)
    env["UPSTOX_API_KEY"] = "unit-test-key"
    env.pop("DATABASE_URL", None)
    env["SYSTEM_MODE"] = "LOCAL"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "preflight.py"), "--json"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=120,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "credentials" in payload["checks"][0]["detail"].lower()


# ---------------------------------------------------------------------------
# AUDIT-027 — a test run must not write into the repository's committed data
# ---------------------------------------------------------------------------


def test_audit_027_settings_data_dir_is_redirected_per_test(
    tmp_path: Path, monkeypatch
) -> None:
    """The isolation fixture must actually move ``settings.storage.data_dir``.

    Setting only ``QUANT_DATA_DIR`` is not enough: the field's default was
    evaluated when ``settings`` was imported, so the singleton kept pointing
    at the committed ``data/`` directory and every test wrote into it.
    """
    from config.settings import settings

    monkeypatch.setattr(settings.storage, "data_dir", tmp_path / "elsewhere")
    assert settings.storage.data_dir == tmp_path / "elsewhere"
    assert settings.storage.duckdb_path == tmp_path / "elsewhere" / "quant.duckdb"
    assert settings.storage.raw_dir == tmp_path / "elsewhere" / "raw"


def test_audit_027_rebind_re_reads_the_environment(
    tmp_path: Path, monkeypatch
) -> None:
    """``rebind()`` is the documented way to honour a changed environment."""
    from config.settings import settings

    monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "rebound"))
    assert settings.storage.rebind() == tmp_path / "rebound"
    assert settings.storage.data_dir == tmp_path / "rebound"


def test_audit_027_storage_layer_does_not_bind_import_time_defaults(
    tmp_path: Path, monkeypatch
) -> None:
    """``StorageManager()`` / ``DuckDBManager()`` must resolve paths lazily.

    Both used ``data_dir: Path = settings.storage.raw_dir`` as a *default
    argument*, which Python evaluates once when the module is imported —
    before any fixture can redirect it.
    """
    from config.settings import settings
    from data.duckdb_manager import DuckDBManager
    from data.storage import StorageManager

    monkeypatch.setattr(settings.storage, "data_dir", tmp_path / "isolated")
    assert StorageManager().data_dir == tmp_path / "isolated" / "raw"
    manager = DuckDBManager()
    assert manager.db_path == tmp_path / "isolated" / "quant.duckdb"
    assert manager.data_dir == tmp_path / "isolated" / "raw"


def test_audit_027_storage_manager_default_is_usable(tmp_path, monkeypatch) -> None:
    """Regression: an intermediate fix left ``StorageManager().data_dir`` as None."""
    from config.settings import settings
    from data.storage import StorageManager

    monkeypatch.setattr(settings.storage, "data_dir", tmp_path / "isolated")
    manager = StorageManager()
    assert manager.data_dir is not None
    assert manager._get_partition_path("yfinance", "NSE", "RELIANCE", 2024, 3).parts[
        -1
    ] == "03.parquet"


# ---------------------------------------------------------------------------
# AUDIT-024 / AUDIT-029 — one source of truth for risk limits
# ---------------------------------------------------------------------------


def test_audit_024_paper_policy_is_derived_from_the_guard() -> None:
    """Two hard-coded copies of the limits must not exist any more."""
    import config.risk_policy as policy
    from paper_trading.service import DEFAULT_RISK_POLICY
    from risk_kill.guard import RiskLimits

    guard = RiskLimits()
    assert DEFAULT_RISK_POLICY is policy.DEFAULT_RISK_POLICY
    assert DEFAULT_RISK_POLICY["max_gross_exposure"] == guard.max_gross_exposure
    assert DEFAULT_RISK_POLICY["daily_loss_limit"] == guard.max_daily_loss
    # Where the two copies disagreed, the *stricter* value must win.
    assert DEFAULT_RISK_POLICY["max_position_weight"] == min(
        0.15, guard.max_position_exposure
    )
    assert DEFAULT_RISK_POLICY["max_drawdown"] == min(0.15, guard.max_drawdown)


def test_audit_024_paper_limits_are_never_looser_than_the_guard() -> None:
    from config.risk_policy import DEFAULT_RISK_POLICY
    from risk_kill.guard import RiskLimits

    guard = RiskLimits()
    assert DEFAULT_RISK_POLICY["max_position_weight"] <= guard.max_position_exposure
    assert DEFAULT_RISK_POLICY["max_drawdown"] <= guard.max_drawdown
    assert DEFAULT_RISK_POLICY["daily_loss_limit"] <= guard.max_daily_loss
    assert DEFAULT_RISK_POLICY["max_gross_exposure"] <= guard.max_gross_exposure


def test_audit_029_staleness_windows_are_defined_together() -> None:
    """The 6-day / 18-hour gap must be explicit, not accidental."""
    import inspect

    import config.risk_policy as policy
    from data.quality import detect_data_staleness

    default = inspect.signature(detect_data_staleness).parameters[
        "max_staleness_days"
    ].default
    assert default is policy.MAX_DATA_AGE_QUALITY_DAYS
    assert policy.MAX_DATA_AGE_HOURS == 18.0
    policy.assert_quality_window_is_consistent()


def test_audit_029_inconsistent_windows_are_rejected(monkeypatch) -> None:
    import config.risk_policy as policy

    monkeypatch.setattr(policy, "MAX_DATA_AGE_QUALITY_DAYS", 0.1)
    with pytest.raises(ValueError, match="shorter than the trading window"):
        policy.assert_quality_window_is_consistent()
    monkeypatch.setattr(policy, "MAX_DATA_AGE_QUALITY_DAYS", 30.0)
    with pytest.raises(ValueError, match="exceeds one week"):
        policy.assert_quality_window_is_consistent()


def test_audit_031_execution_sample_matches_order_result() -> None:
    """Every record in execution/orders.jsonl must build a real OrderResult."""
    from models.domain import OrderResult

    path = ROOT / "execution" / "orders.jsonl"
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert records, "the sample execution log is empty"
    forbidden = {"price", "expected_price", "actual_price", "quantity"}
    for record in records:
        assert not forbidden & set(record), f"stale keys in {record}"
        OrderResult(**record)


# ---------------------------------------------------------------------------
# AUDIT-033 — Streamlit is declared and its absence is actionable
# ---------------------------------------------------------------------------


def test_audit_033_streamlit_is_declared_as_an_extra() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dashboards = [" in text
    assert "streamlit>=" in text


def test_audit_033_missing_streamlit_gives_an_actionable_error() -> None:
    """Importing a dashboard module must not explode; rendering must explain."""
    from dashboard.streamlit_guard import (
        INSTALL_HINT,
        MISSING_STREAMLIT,
        require_streamlit,
    )

    with pytest.raises(RuntimeError) as excinfo:
        _ = MISSING_STREAMLIT.sidebar
    assert "streamlit is not installed" in str(excinfo.value)
    assert ".[dashboards]" in INSTALL_HINT

    # When streamlib *is* importable the helper returns the real module.
    import sys
    import types

    fake = types.ModuleType("streamlit")
    monkeypatched = sys.modules.get("streamlit")
    sys.modules["streamlit"] = fake
    try:
        assert require_streamlit() is fake
    finally:
        if monkeypatched is None:
            del sys.modules["streamlit"]
        else:
            sys.modules["streamlit"] = monkeypatched


# ---------------------------------------------------------------------------
# AUDIT-036 — the documented environment must be legal
# ---------------------------------------------------------------------------


def test_audit_036_documented_setup_passes_the_validator(monkeypatch) -> None:
    """A clean checkout that follows .env.example must not fail preflight."""
    from config.env_validator import validate_environment

    for name in (
        "UPSTOX_API_KEY",
        "UPSTOX_API_SECRET",
        "DHAN_CLIENT_ID",
        "DHAN_API_SECRET",
        "DATABASE_URL",
        "SYSTEM_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SYSTEM_MODE=LOCAL" in example

    # No SYSTEM_MODE at all must assume LOCAL (the .env.example default),
    # which needs no DATABASE_URL — this is what used to raise.
    validate_environment()


def test_audit_036_example_does_not_advertise_fatal_variables() -> None:
    """.env.example must warn that the broker app credentials are fatal."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    lowered = example.lower()
    assert "do not set these" in lowered
    assert "# upstox_api_key=" in lowered


def test_audit_036_secrets_doc_does_not_require_broker_credentials() -> None:
    text = (ROOT / "docs" / "secrets_management.md").read_text(encoding="utf-8")
    assert "must **not** be set" in text
    assert "UPSTOX_API_KEY" in text


# ---------------------------------------------------------------------------
# AUDIT-037 — the container must be able to write where the app writes
# ---------------------------------------------------------------------------


def test_audit_037_data_mount_is_writable() -> None:
    """``/api/data/rebuild-prices`` writes into data/, so :ro breaks it."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./data:/app/data:ro" not in text
    assert "./data:/app/data" in text


# ---------------------------------------------------------------------------
# AUDIT-013 / AUDIT-034 — the paper gate must say why it is closed
# ---------------------------------------------------------------------------


def test_audit_013_momrem_is_in_the_paper_registry() -> None:
    """The only strategy with a target builder was missing from the file."""
    payload = json.loads((ROOT / "config" / "paper_strategies.json").read_text())
    entry = payload["strategies"]["momrem"]
    assert entry["paper_approved"] is False
    assert entry["reason"], "the registry must explain the refusal"


def test_audit_034_paper_service_publishes_the_blocked_reason(tmp_path) -> None:
    from paper_trading.service import PaperTradingService

    service = PaperTradingService(root=tmp_path)
    status = service.status()
    assert "rebalance_blockers" in status
    assert "rebalance_blocked_reason" in status
    blockers = service.rebalance_blockers()
    assert any("not paper-approved" in item for item in blockers)


def test_audit_034_approved_strategy_reports_no_blockers(tmp_path) -> None:
    from paper_trading.service import PaperTradingService

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "paper_strategies.json").write_text(
        json.dumps(
            {
                "strategies": {
                    "momrem": {
                        "label": "test",
                        "status": "PAPER_APPROVED",
                        "paper_approved": True,
                        "mode": "DAILY",
                        "min_rebalance_seconds": 86400,
                        "reason": "",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    service = PaperTradingService(root=tmp_path)
    service.ledger.start(None)
    assert service.rebalance_blockers() == []


# ---------------------------------------------------------------------------
# AUDIT-011 — the calendar must know NSE's real sessions, not just weekdays
# ---------------------------------------------------------------------------


def test_audit_011_special_sessions_are_trading_days() -> None:
    """Weekend sessions the exchange really held must not be flagged.

    Each date below was verified against NSE/BSE circulars and press
    coverage on 2026-09-02. Before they were added, real prices for these
    sessions were reported as corrupt data: 186 rows across 3 dates in a
    120-symbol sample of the committed eod2 source.
    """
    from data.quality import nse_trading_calendar

    calendar = nse_trading_calendar()
    for day, label in (
        ("2024-01-20", "DR switchover session (Saturday)"),
        ("2024-03-02", "DR switchover session (Saturday)"),
        ("2024-05-18", "DR switchover session (Saturday)"),
        ("2025-02-01", "Union Budget 2025 (Saturday)"),
        ("2025-10-21", "Diwali Muhurat trading (a published holiday)"),
        ("2026-02-01", "Union Budget 2026 (Sunday)"),
    ):
        assert calendar.is_trading_day(pd.Timestamp(day).date()), (
            f"{day} ({label}) must be recognised as a trading day"
        )


def test_audit_011_published_holidays_are_not_trading_days() -> None:
    from data.quality import nse_trading_calendar

    calendar = nse_trading_calendar()
    for day in ("2024-01-22", "2024-01-26", "2024-03-08", "2025-10-22"):
        assert not calendar.is_trading_day(pd.Timestamp(day).date()), day


def test_audit_011_the_calendar_is_committed_and_dated() -> None:
    import json

    payload = json.loads(
        (ROOT / "data" / "calendar" / "nse_trading_calendar.json").read_text()
    )
    assert payload["exchange"] == "NSE"
    assert payload["segment"] == "CM"
    assert payload["retrieved_at"]
    assert payload["sources"], "provenance must be recorded"
    assert len(payload["holidays"]) >= 50
    assert len(payload["special_sessions"]) >= 6


def test_audit_011_calendar_ships_in_the_wheel() -> None:
    """AUDIT-016 follow-up: the package-data entry must match the file."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'data = ["calendar/*.json"]' in text
