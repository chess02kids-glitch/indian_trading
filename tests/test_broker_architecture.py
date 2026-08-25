"""Broker-layer architecture audit.

Verifies the boundaries that keep the sandbox integration safe:

* ``risk_kill`` remains untouched (stdlib-only, no broker/auth imports).
* No broker SDK or network coupling anywhere in the broker path.
* Research-side packages (research/portfolio/backtest/agents) never import
  the broker layer — research code cannot call a broker.
* Repository interfaces and the execution adapter protocol are preserved:
  the sandbox executor is a drop-in ``ExecutionAdapter``.
* Paper mode is unchanged and no live order path exists.
* No secrets are embedded in broker sources.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Top-level modules the broker package must never import directly.
FORBIDDEN_BROKER_IMPORTS = {
    # network clients / sockets
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "urllib3",
    # real broker SDKs
    "upstox",
    "dhanhq",
    "kiteconnect",
    "upstox_client",
    # AI/agent stack
    "agents",
    "langchain",
    "openai",
    "anthropic",
    # research side of the wall
    "research",
    "portfolio",
    "backtest",
}

#: Packages that must never reach the broker layer.
RESEARCH_SIDE_PACKAGES = ("research", "portfolio", "backtest", "agents")

_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passphrase)\b"
    r"\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"
)


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _package_files(name: str) -> list[Path]:
    return sorted((ROOT / name).rglob("*.py"))


class TestBrokerPackageBoundaries:
    def test_broker_imports_no_network_or_sdk_modules(self) -> None:
        for path in _package_files("broker"):
            offenders = _imported_modules(path.read_text(encoding="utf-8")) & (
                FORBIDDEN_BROKER_IMPORTS
            )
            assert not offenders, f"broker/{path.name} imports {sorted(offenders)}"

    def test_research_side_never_imports_broker(self) -> None:
        """Research code must never call broker adapters directly."""
        for package in RESEARCH_SIDE_PACKAGES:
            for path in _package_files(package):
                imported = _imported_modules(path.read_text(encoding="utf-8"))
                assert "broker" not in imported, (
                    f"{package}/{path.name} imports the broker layer"
                )
                assert "auth" not in imported or package == "agents", (
                    f"{package}/{path.name} imports broker auth"
                )

    def test_execution_never_imports_broker_layer(self) -> None:
        for path in _package_files("execution"):
            if path.name == "__init__.py":
                continue
            imported = _imported_modules(path.read_text(encoding="utf-8"))
            assert "broker" not in imported
            assert "auth" not in imported


class TestRiskKillUntouched:
    def test_risk_kill_has_no_broker_coupling(self) -> None:
        for path in _package_files("risk_kill"):
            imported = _imported_modules(path.read_text(encoding="utf-8"))
            assert "broker" not in imported
            assert "auth" not in imported

    def test_risk_kill_stdlib_only(self) -> None:
        standard = set(getattr(__import__("sys"), "stdlib_module_names", ()))
        for path in _package_files("risk_kill"):
            for module in _imported_modules(path.read_text(encoding="utf-8")):
                assert module in standard or module == "models"


class TestExecutionProtocolPreserved:
    def test_sandbox_executor_satisfies_execution_adapter(self, tmp_path) -> None:
        from broker.safe_execution import SandboxExecutionAdapter
        from execution.adapter import ExecutionAdapter
        from tests.sandbox_common import SandboxEnv

        env = SandboxEnv(tmp_path, "upstox")
        executor = SandboxExecutionAdapter(env.adapter, sleep=lambda s: None)
        assert isinstance(executor, ExecutionAdapter)

    def test_repository_protocols_unchanged(self) -> None:
        """The store protocols still describe the same repository surface."""
        from store.memory import (
            InMemoryOrderRepository,
            InMemoryPositionRepository,
            InMemoryReconciliationRepository,
        )
        from store.protocols import (
            OrderRepository,
            PositionRepository,
            ReconciliationRepository,
        )

        assert isinstance(InMemoryOrderRepository(), OrderRepository)
        assert isinstance(InMemoryPositionRepository(), PositionRepository)
        assert isinstance(InMemoryReconciliationRepository(), ReconciliationRepository)

    def test_domain_mode_still_excludes_live(self) -> None:
        from models.domain import ExecutionMode

        assert {member.name for member in ExecutionMode} == {
            "RESEARCH",
            "PAPER",
            "SANDBOX",
        }


class TestPaperModeUnchanged:
    def test_paper_broker_still_fills_deterministically(self) -> None:
        from datetime import UTC, datetime

        from execution.paper import PaperBroker, PaperBrokerConfig
        from models.domain import OrderStatus
        from tests.sandbox_common import make_intent

        broker = PaperBroker(
            datetime(2026, 8, 25, 9, 15, tzinfo=UTC),
            PaperBrokerConfig(
                seed=7, fill_probability=1.0, partial_fill_probability=0.0
            ),
        )
        result = broker.submit_order(make_intent("ord-1"), reference_price=100.0)
        assert result.status is OrderStatus.FILLED
        assert broker.get_cash() == 1_000_000.0 - 1000.0

    def test_paper_broker_is_not_broker_package_code(self) -> None:
        import execution.paper as paper_module

        imported = _imported_modules(Path(paper_module.__file__).read_text())
        assert "broker" not in imported


class TestNoLiveOrderPath:
    @pytest.mark.parametrize("path", _package_files("broker"), ids=lambda p: p.name)
    def test_broker_sources_contain_no_http_client_calls(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        for call in ("urlopen(", "urlretrieve(", ".connect(", "getaddrinfo("):
            assert call not in text, f"{path.name} performs network call {call}"

    def test_http_stub_can_never_execute(self) -> None:
        from broker.transport import HttpSandboxTransportStub

        stub = HttpSandboxTransportStub("https://sandbox.dhan.co")
        with pytest.raises(Exception):
            stub.request("place", payload={})
        with pytest.raises(Exception):
            stub.exchange_code("code")

    @pytest.mark.parametrize("path", _package_files("broker"), ids=lambda p: p.name)
    def test_no_hardcoded_credentials_in_broker_sources(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        match = _SECRET_RE.search(text)
        assert match is None, f"possible hardcoded secret in {path}"
