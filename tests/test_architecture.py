"""CI architecture-boundary tests.

These tests enforce, in code, the rules that keep the system safe:

* ``risk_kill`` imports nothing outside the standard library (no agents,
  langchain, langgraph, ollama, or any LLM/model-serving library) — so AI
  code can never influence kill-switch decisions.
* No execution/live-broker coupling exists: the only order type is LIMIT,
  and no execution module opens network connections.
* No secrets are embedded in source code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Modules that must never be reachable from risk_kill (AI/agent stack).
FORBIDDEN_RISK_KILL_MODULES = {
    "agents",
    "langchain",
    "langchain_core",
    "langchain_community",
    "langgraph",
    "ollama",
    "openai",
    "anthropic",
    "google.generativeai",
    "torch",
    "transformers",
    "genkit",
    "crewai",
    "autogen",
    "pinecone",
    "chromadb",
    "sentence_transformers",
}

#: Modules that must never be imported anywhere in the execution path
#: (network/broker/live-trading coupling).
FORBIDDEN_EXECUTION_MODULES = {
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "upstox",
    "dhanhq",
    "kiteconnect",
    "broker",
    "auth",
}

# AUDIT-003: the previous pattern anchored the keyword with ``\b``. ``\b``
# cannot match between ``_`` and a letter, so ``FRED_API_KEY = "…32 hex chars"``
# (scripts/ingest_macro.py) was invisible to this test while a live FRED API
# key sat in the repository. The lookarounds below deliberately allow a leading
# ``_`` (so ``FRED_API_KEY`` / ``MY_SECRET`` are covered) while still refusing
# ``api_keys`` / ``tokenize`` / ``passwordless``.
_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(api[_-]?key|secret|token|password|passphrase)"
    r"(?![A-Za-z0-9])"
    r"\s*[:=]\s*['\"]([A-Za-z0-9_\-]{16,})['\"]"
)

#: Placeholder-looking values (docs, examples, env-var *names*) are not secrets.
_SECRET_PLACEHOLDER_RE = re.compile(
    r"(?i)^(your|my|the|replace|changeme|example|sample|dummy|fake|placeholder|"
    r"xxx+|todo|<.*>|\*{3,})"
)


def _looks_like_secret(value: str) -> bool:
    """Heuristic: a real credential mixes character classes and is not a name.

    ``"TELEGRAM_BOT_TOKEN"`` (an environment-variable *name* assigned to
    ``_ENV_TOKEN`` in a test) must not trip the scanner, while
    ``FRED_API_KEY = "<32 hex chars>"`` must.
    """
    if _SECRET_PLACEHOLDER_RE.match(value):
        return False
    if value.isupper() and "_" in value:  # ENV_VAR_NAME, not a value
        return False
    has_digit = any(char.isdigit() for char in value)
    has_alpha = any(char.isalpha() for char in value)
    mixed_case = any(char.islower() for char in value) and any(
        char.isupper() for char in value
    )
    return (has_digit and has_alpha) or mixed_case or len(value) >= 40


def _imported_modules(source: str) -> set[str]:
    """Absolute top-level modules imported by ``source`` (relative imports
    stay inside the importing package and are excluded)."""
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


def _package_modules(package_dir: Path) -> dict[str, str]:
    """Map module dotted-name -> source for every file in a package."""
    modules: dict[str, str] = {}
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(ROOT)
        name = ".".join(relative.with_suffix("").parts)
        modules[name] = path.read_text(encoding="utf-8")
    return modules


class TestRiskKillBoundary:
    """Final suite (test 1): the risk package cannot import AI."""

    def test_risk_kill_imports_standard_library_only(self) -> None:
        modules = _package_modules(ROOT / "risk_kill")
        assert modules, "risk_kill package has no python files"
        standard = set(getattr(__import__("sys"), "stdlib_module_names", ()))
        for dotted, source in modules.items():
            for module in _imported_modules(source):
                # The only permitted non-stdlib import is models.domain
                # (risk_kill.RiskDecision.to_model), and models.domain is
                # itself checked for AI imports by the closure test below.
                assert module in standard or module == "models", (
                    f"risk_kill module {dotted} imports non-stdlib module {module!r}"
                )

    def test_risk_kill_import_closure_has_no_ai(self) -> None:
        """Walk the full import closure from risk_kill; no AI modules may appear."""
        available = _package_modules(ROOT / "risk_kill")
        available.update(_package_modules(ROOT / "models"))
        top_level_to_submodules: dict[str, list[str]] = {}
        for dotted in available:
            top = dotted.split(".")[0]
            top_level_to_submodules.setdefault(top, []).append(dotted)

        seen: set[str] = set()
        stack = [dotted for dotted in available if dotted.startswith("risk_kill")]
        while stack:
            dotted = stack.pop()
            if dotted in seen:
                continue
            seen.add(dotted)
            for module in _imported_modules(available[dotted]):
                assert module not in FORBIDDEN_RISK_KILL_MODULES, (
                    f"AI module {module!r} reachable from risk_kill via {dotted}"
                )
                if module in top_level_to_submodules:
                    stack.extend(top_level_to_submodules[module])

    def test_risk_kill_source_mentions_no_ai_frameworks(self) -> None:
        for path in (ROOT / "risk_kill").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for framework in (
                "langchain",
                "langgraph",
                "ollama",
                "openai",
                "anthropic",
                "torch",
                "transformers",
            ):
                assert framework not in text.lower(), (
                    f"{path.name} references AI framework {framework!r}"
                )


class TestExecutionBoundary:
    def test_execution_modules_make_no_network_calls(self) -> None:
        for path in (ROOT / "execution").rglob("*.py"):
            if path.name == "__init__.py":
                continue
            modules = _imported_modules(path.read_text(encoding="utf-8"))
            offenders = modules & FORBIDDEN_EXECUTION_MODULES
            assert not offenders, (
                f"execution module {path.name} imports {sorted(offenders)}"
            )

    def test_order_type_is_limit_only(self) -> None:
        from models.domain import OrderType

        assert [member.name for member in OrderType] == ["LIMIT"]

    def test_execution_mode_has_no_live(self) -> None:
        from models.domain import ExecutionMode

        assert "LIVE" not in {member.name for member in ExecutionMode}


class TestNoSecretsInSource:
    @pytest.mark.parametrize(
        "path",
        [
            p
            for p in ROOT.rglob("*.py")
            if ".venv" not in p.parts and ".git" not in p.parts
        ],
        ids=lambda p: str(p.relative_to(ROOT)),
    )
    def test_no_hardcoded_credentials(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        match = _SECRET_RE.search(text)
        if match is not None and _looks_like_secret(match.group(2)):
            pytest.fail(
                f"possible hardcoded secret in {path}: "
                f"{match.group(1)!r} assigned a credential-looking literal"
            )
