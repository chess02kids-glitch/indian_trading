"""Feature-flag tests: execution modes and the disabled LIVE path."""

from __future__ import annotations

import pytest

from broker.errors import LiveTradingDisabledError
from broker.mode import (
    EXECUTION_MODE_ENV,
    OperatingMode,
    check_execution_permitted,
    resolve_operating_mode,
    to_execution_mode,
)
from models.domain import ExecutionMode


class TestResolveOperatingMode:
    def test_default_is_research(self) -> None:
        assert resolve_operating_mode({}) is OperatingMode.RESEARCH

    def test_explicit_modes_resolve(self) -> None:
        for raw, mode in (
            ("research", OperatingMode.RESEARCH),
            ("PAPER", OperatingMode.PAPER),
            ("Sandbox", OperatingMode.SANDBOX),
            ("live", OperatingMode.LIVE),
        ):
            assert resolve_operating_mode({EXECUTION_MODE_ENV: raw}) is mode

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            resolve_operating_mode({EXECUTION_MODE_ENV: "crypto"})

    def test_blank_value_uses_default(self) -> None:
        assert (
            resolve_operating_mode({EXECUTION_MODE_ENV: "  "}) is OperatingMode.RESEARCH
        )


class TestExecutionModeMapping:
    def test_sandbox_maps_to_domain(self) -> None:
        assert to_execution_mode(OperatingMode.SANDBOX) is ExecutionMode.SANDBOX

    def test_paper_maps_to_domain(self) -> None:
        assert to_execution_mode(OperatingMode.PAPER) is ExecutionMode.PAPER

    def test_research_maps_to_domain(self) -> None:
        assert to_execution_mode(OperatingMode.RESEARCH) is ExecutionMode.RESEARCH

    def test_live_has_no_domain_mapping(self) -> None:
        with pytest.raises(LiveTradingDisabledError):
            to_execution_mode(OperatingMode.LIVE)


class TestExecutionPermitted:
    def test_sandbox_permitted(self) -> None:
        assert check_execution_permitted(OperatingMode.SANDBOX) is ExecutionMode.SANDBOX

    def test_paper_permitted(self) -> None:
        assert check_execution_permitted(OperatingMode.PAPER) is ExecutionMode.PAPER

    def test_live_refuses_execution(self) -> None:
        """LIVE mode must refuse execution — always."""
        with pytest.raises(LiveTradingDisabledError, match="LIVE"):
            check_execution_permitted(OperatingMode.LIVE)

    def test_live_string_refuses_execution(self) -> None:
        with pytest.raises(LiveTradingDisabledError):
            check_execution_permitted("LIVE")

    def test_research_refuses_execution(self) -> None:
        with pytest.raises(LiveTradingDisabledError, match="RESEARCH"):
            check_execution_permitted(OperatingMode.RESEARCH)

    def test_unknown_mode_refuses(self) -> None:
        with pytest.raises(LiveTradingDisabledError):
            check_execution_permitted("turbo")


class TestDomainBoundary:
    def test_domain_execution_mode_has_no_live(self) -> None:
        """The domain enum deliberately excludes LIVE (architecture invariant)."""
        assert "LIVE" not in {member.name for member in ExecutionMode}

    def test_execution_permitted_property(self) -> None:
        assert OperatingMode.SANDBOX.execution_permitted
        assert OperatingMode.PAPER.execution_permitted
        assert not OperatingMode.RESEARCH.execution_permitted
        assert not OperatingMode.LIVE.execution_permitted
