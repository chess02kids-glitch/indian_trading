"""v0.5 runner contract tests that need no database or network."""
from __future__ import annotations

import ast
from pathlib import Path


def test_daily_runner_is_paper_only_and_has_deterministic_exit_codes() -> None:
    source = Path("scripts/run_daily.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    codes = next(
        node.value for node in assignments
        if any(isinstance(target, ast.Name) and target.id == "EXIT_CODES" for target in node.targets)
    )
    assert isinstance(codes, ast.Dict)
    values = {key.value: value.value for key, value in zip(codes.keys, codes.values, strict=True)}
    assert values == {"completed": 0, "duplicate_run": 10, "halted_data_quality": 20,
                      "halted_risk": 21, "awaiting_approval": 22,
                      "locked_reconciliation": 23, "unexpected_failure": 70}
    assert 'not in {"", "PAPER"}' in source
    assert "PaperBroker" in source
    assert "LIVE" not in source


def test_daily_service_uses_only_the_canonical_paper_runner() -> None:
    service = Path("deploy/systemd/quant-india-daily.service").read_text(encoding="utf-8")
    timer = Path("deploy/systemd/quant-india-daily.timer").read_text(encoding="utf-8")
    assert "scripts/run_daily.py" in service
    assert "--approved-by" not in service
    assert "quant-india-daily.service" in timer
