"""Broker CLI: health/login/funds/holdings/sandbox-order/reconcile flows."""

from __future__ import annotations

import json

import pytest

from broker.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_REJECTED,
    build_parser,
    cli_main,
)
from broker.simulated import PendingFault, RejectFault
from tests.sandbox_common import SandboxEnv


class CliRig:
    """Wires the CLI to hermetic sandbox adapters in one tmp directory."""

    def __init__(self, tmp_path) -> None:
        self.envs: dict[str, SandboxEnv] = {
            name: SandboxEnv(tmp_path / name, name) for name in ("upstox", "dhan")
        }
        self.environ = {
            "QUANT_BROKER_STATUS_FILE": str(tmp_path / "var" / "broker_status.json"),
        }
        self.lines: list[str] = []

    def factory(self, broker: str):
        return self.envs[broker].adapter

    def out(self, line: str) -> None:
        self.lines.append(line)

    def run(self, *argv: str) -> int:
        return cli_main(
            list(argv), factory=self.factory, environ=self.environ, out=self.out
        )

    def parsed_output(self, start=0):
        return json.loads("\n".join(self.lines[start:]))

    def status_doc(self):
        return load_status(self.environ)

    def place(self, *extra: str) -> int:
        return self.run(
            "sandbox-order",
            "upstox",
            "--symbol",
            "RELIANCE",
            "--side",
            "BUY",
            "--quantity",
            "5",
            "--limit-price",
            "100",
            "--reference-price",
            "100",
            *extra,
        )


def load_status(environ):
    from broker.status import load_prior_status

    return load_prior_status(environ)


@pytest.fixture
def rig(tmp_path) -> CliRig:
    return CliRig(tmp_path)


class TestParser:
    def test_no_command_prints_help(self, rig) -> None:
        assert rig.run() == EXIT_OK

    def test_parser_has_all_commands(self) -> None:
        parser = build_parser()
        assert parser.prog == "broker"


class TestModeGate:
    def test_live_mode_refuses_everything(self, rig) -> None:
        rig.environ["QUANT_EXECUTION_MODE"] = "LIVE"
        assert rig.run("health") == EXIT_REFUSED
        assert any("LIVE" in line for line in rig.lines)

    def test_live_mode_refuses_orders(self, rig) -> None:
        rig.environ["QUANT_EXECUTION_MODE"] = "live"
        assert rig.place() == EXIT_REFUSED

    def test_research_mode_refuses_orders_but_allows_reads(self, rig) -> None:
        rig.environ["QUANT_EXECUTION_MODE"] = "RESEARCH"
        assert rig.place() == EXIT_REFUSED
        assert rig.run("orders", "upstox") == EXIT_OK

    def test_paper_mode_refuses_sandbox_orders(self, rig) -> None:
        rig.environ["QUANT_EXECUTION_MODE"] = "PAPER"
        assert rig.place() == EXIT_REFUSED

    def test_default_mode_is_sandbox_for_cli(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.place() == EXIT_OK


class TestHealthAndStatus:
    def test_health_all_writes_status_document(self, rig) -> None:
        assert rig.run("health") == EXIT_OK
        doc = rig.parsed_output()
        assert set(doc["broker_connectivity"]) == {"dhan", "upstox"}
        assert doc["sandbox_health"] == "degraded"  # tokens missing before login
        persisted = rig.status_doc()
        assert persisted["sandbox_health"] == "degraded"

    def test_health_single_broker(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.run("health", "--broker", "upstox") == EXIT_OK
        doc = rig.parsed_output()
        assert doc["sandbox_health"] == "healthy"
        assert doc["token_status"]["upstox"]["state"] == "active"


class TestLogin:
    def test_login_without_code_prints_url(self, rig) -> None:
        assert rig.run("login", "upstox") == EXIT_OK
        joined = "\n".join(rig.lines)
        assert "simulated://upstox/oauth/authorize" in joined
        assert "--code" in joined

    def test_login_with_code_stores_masked_token(self, rig) -> None:
        assert rig.run("login", "upstox", "--code", "human-code") == EXIT_OK
        doc = rig.parsed_output()
        assert doc["authenticated"] is True
        assert doc["token_state"] == "active"
        raw = rig.envs["upstox"].adapter.token_manager.get_token("upstox")
        assert raw not in json.dumps(doc)
        assert doc["masked_token"] is not None


class TestReads:
    def test_funds_requires_login(self, rig) -> None:
        assert rig.run("funds", "upstox") == EXIT_ERROR
        assert any("funds failed" in line for line in rig.lines)

    def test_funds_after_login(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.run("funds", "upstox") == EXIT_OK
        doc = rig.parsed_output()
        assert doc["available_cash"] == pytest.approx(1_000_000.0)

    def test_holdings_after_order(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.place() == EXIT_OK
        rig.lines.clear()
        assert rig.run("holdings", "upstox") == EXIT_OK
        holdings = rig.parsed_output()
        assert [(h["symbol"], h["quantity"]) for h in holdings] == [("RELIANCE", 5)]

    def test_orders_lists_recent(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.place() == EXIT_OK
        rig.lines.clear()
        assert rig.run("orders", "upstox") == EXIT_OK
        doc = rig.parsed_output()
        assert len(doc["recent_sandbox_orders"]) == 1
        assert doc["recent_sandbox_orders"][0]["symbol"] == "RELIANCE"


class TestSandboxOrder:
    def test_order_fills_and_persists(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.place() == EXIT_OK
        doc = rig.parsed_output()
        assert doc["mode"] == "SANDBOX"
        assert doc["decision"] == "NOMINAL"
        assert doc["order"]["status"] == "FILLED"
        assert doc["order"]["filled_quantity"] == 5
        persisted = rig.status_doc()
        assert persisted["recent_sandbox_orders"][0]["status"] == "FILLED"

    def test_order_dhan(self, rig) -> None:
        rig.envs["dhan"].login()
        code = rig.run(
            "sandbox-order",
            "dhan",
            "--symbol",
            "SBIN",
            "--side",
            "BUY",
            "--quantity",
            "3",
            "--limit-price",
            "100",
            "--reference-price",
            "100",
        )
        assert code == EXIT_OK
        doc = rig.parsed_output()
        assert (doc["order"]["broker_order_id"] or "").startswith("dhan-")

    def test_order_rejected_by_broker_exit_2(self, rig) -> None:
        rig.envs["upstox"].login()
        rig.envs["upstox"].transport.script("place", [RejectFault("bad symbol")])
        assert rig.place() == EXIT_REJECTED
        doc = rig.parsed_output()
        assert doc["order"]["status"] == "REJECTED"

    def test_order_band_refusal_exit_3(self, rig) -> None:
        """A limit far from the reference price is refused before submission."""
        rig.envs["upstox"].login()
        code = rig.run(
            "sandbox-order",
            "upstox",
            "--symbol",
            "RELIANCE",
            "--side",
            "BUY",
            "--quantity",
            "5",
            "--limit-price",
            "100",
            "--reference-price",
            "150",
        )
        assert code == EXIT_REFUSED
        doc = rig.parsed_output()
        assert doc["refused"] is True
        assert "band" in doc["reason"]

    def test_risk_guard_refusal_when_disconnected(self, rig) -> None:
        from broker.simulated import TimeoutFault

        rig.envs["upstox"].login()
        rig.envs["upstox"].transport.script("ping", [TimeoutFault()] * 2)
        assert rig.place() == EXIT_REFUSED
        doc = rig.parsed_output()
        assert doc["refused"] is True
        assert "STOP_NEW_ORDERS" in doc["reason"]

    def test_unknown_exit_when_broker_unreachable(self, rig) -> None:
        from broker.simulated import TimeoutFault

        rig.envs["upstox"].login()
        rig.envs["upstox"].transport.script("place", [TimeoutFault()] * 3)
        assert rig.place() == EXIT_REJECTED
        doc = rig.parsed_output()
        assert doc["order"]["status"] == "UNKNOWN"


class TestCancel:
    def test_cancel_pending_order(self, rig) -> None:
        rig.envs["upstox"].login()
        rig.envs["upstox"].transport.script("place", [PendingFault(polls=99)])
        assert rig.place() == EXIT_OK
        doc = rig.parsed_output()
        internal = doc["order"]["internal_order_id"]
        rig.lines.clear()
        assert rig.run("sandbox-cancel", "upstox", "--internal-id", internal) == EXIT_OK
        cancelled = rig.parsed_output()
        assert cancelled["order"]["status"] == "CANCELLED"

    def test_cancel_unknown_id(self, rig) -> None:
        rig.envs["upstox"].login()
        assert (
            rig.run("sandbox-cancel", "upstox", "--internal-id", "ghost") == EXIT_ERROR
        )


class TestReconcile:
    def test_reconcile_matched_after_orders(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.place() == EXIT_OK
        rig.lines.clear()
        assert rig.run("reconcile", "upstox") == EXIT_OK
        doc = rig.parsed_output()
        assert doc["matched"] is True
        assert doc["risk_state"] == "NOMINAL"
        persisted = rig.status_doc()
        assert persisted["reconciliation_health"]["state"] == "matched"

    def test_reconcile_detects_broker_drift_and_locks(self, rig) -> None:
        rig.envs["upstox"].login()
        assert rig.place() == EXIT_OK
        # operator-visible drift on the broker side: the position vanishes
        rig.envs["upstox"].backend()._positions.clear()
        rig.lines.clear()
        assert rig.run("reconcile", "upstox") == EXIT_REFUSED
        doc = rig.parsed_output()
        assert doc["locked"] is True
        assert doc["risk_state"] == "LOCK_ACCOUNT"
        assert any(m["kind"] != "" for m in doc["mismatches"])
        persisted = rig.status_doc()
        assert persisted["reconciliation_health"]["state"] == "locked"


class TestMainEntry:
    def test_main_routes_broker_command(self, tmp_path, monkeypatch) -> None:
        import main as main_module

        monkeypatch.setenv(
            "QUANT_BROKER_STATUS_FILE", str(tmp_path / "var" / "status.json")
        )
        monkeypatch.setenv("QUANT_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("QUANT_EXECUTION_MODE", "SANDBOX")
        with pytest.raises(SystemExit) as exc:
            main_module.main(["broker", "health", "--broker", "upstox"])
        assert exc.value.code == EXIT_OK

    def test_main_help_still_works(self, capsys) -> None:
        import main as main_module

        main_module.main([])
        captured = capsys.readouterr()
        assert "Safe no-op entry point" in captured.out
