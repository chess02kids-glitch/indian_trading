"""AI research boundary tests: proposals in, validated hypotheses out.

The AI interface must never execute code, never see holdout results by
default, never reach execution/broker/risk modules, and never bypass the
campaign budget or the gate. These tests enforce that boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research.ai_research import (
    AIResearchInterface,
    ProposalVerdict,
    ResearchContextBuilder,
    hypothesis_to_experiment,
)
from research.campaign import CampaignStore, ResearchBudget
from research.ledger import HypothesisLedger


def make_proposal(**overrides):
    payload = {
        "strategy_family": "momentum",
        "strategy_id": "momentum",
        "objective": "test proposal",
        "economic_rationale": "momentum persists",
        "expected_mechanism": "autocorrelation",
        "novelty_reason": "first",
        "features": ["close"],
        "transformations": ["momentum_63"],
        "parameters": {"lookback": 63},
        "expected_failure_modes": ["turnover"],
        "confidence": 0.5,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def interface(tmp_path):
    ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
    campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
    campaigns.create_campaign("boundary", ["momentum", "quality"])
    return AIResearchInterface(ledger, campaigns)


def _module_imports(path: Path) -> set[str]:
    """Direct import names of one module (absolute or dotted)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _transitive_imports(root: str) -> set[str]:
    """Resolve the full research.* import graph of ``root`` via AST.

    Deterministic and immune to test-session module pollution: the graph
    is computed from source, not from ``sys.modules``.
    """
    research_dir = Path("research")
    visited: set[str] = set()
    stack = [root]
    while stack:
        module = stack.pop()
        if module in visited:
            continue
        visited.add(module)
        parts = module.split(".")
        if parts[0] != "research":
            continue  # stdlib / third-party leaves
        path = research_dir / Path(*parts[1:]).with_suffix(".py")
        if not path.exists():
            continue
        for imported in _module_imports(path):
            if imported.startswith("research."):
                stack.append(imported)
            elif not imported.startswith(".") and "." not in imported:
                stack.append(f"research.{imported}")
            else:
                # relative import: resolve against the module's package
                package = ".".join(parts[:-1])
                level = len(imported) - len(imported.lstrip("."))
                target = imported.lstrip(".")
                if level == 1:
                    stack.append(f"{package}.{target}")
                elif level == 2:
                    stack.append(f"{'.'.join(parts[:-2])}.{target}")
    return visited


class TestImportBoundary:
    def test_ai_module_never_imports_execution_stack(self) -> None:
        graph = _transitive_imports("research.ai_research")
        forbidden = ("execution", "broker", "risk_kill", "agents", "dashboard")
        violations = sorted(
            name
            for name in graph
            if any(name.startswith(prefix) for prefix in forbidden)
        )
        assert violations == []

    def test_ai_module_never_reaches_measurement_stack(self) -> None:
        graph = _transitive_imports("research.ai_research")
        assert "research.gate" not in graph
        assert not any(name.startswith("backtest") for name in graph)
        # The deterministic engine boundary: runner/zoo/strategies are the
        # only computation the validated hypothesis can reach.
        assert "research.registry" in graph
        assert "research.hypotheses" in graph

    def test_ai_module_has_no_code_execution_constructs(self) -> None:
        path = Path("research/ai_research.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dangerous = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = node.func
                name = (
                    function.id
                    if isinstance(function, ast.Name)
                    else function.attr
                    if isinstance(function, ast.Attribute)
                    else ""
                )
                if name in {
                    "exec",
                    "eval",
                    "compile",
                    "pickle",
                    "yaml",
                    "subprocess",
                    "os",
                    "system",
                    "importlib",
                    "open",
                }:
                    dangerous.add(name)
        assert dangerous == set()

    def test_registry_rejects_code_shaped_strategy_ids(self) -> None:
        interface = interface_fixture()
        result = interface.submit_proposal(
            make_proposal(strategy_id="__import__('os').system"),
            campaign_id=interface.campaigns.list_campaigns()[0].campaign_id,
        )
        assert result.verdict == ProposalVerdict.REJECTED_INVALID

    def test_registry_rejects_unknown_ids(self) -> None:
        interface = interface_fixture()
        campaign_id = interface.campaigns.list_campaigns()[0].campaign_id
        result = interface.submit_proposal(
            make_proposal(strategy_id="llm_generated_alpha"),
            campaign_id=campaign_id,
        )
        assert result.verdict == ProposalVerdict.REJECTED_INVALID
        assert isinstance(result.reason, str)


def interface_fixture(tmp_path=None):
    import tempfile

    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    ledger = HypothesisLedger(root / "ledger.jsonl")
    campaigns = CampaignStore(root / "campaigns.jsonl")
    campaigns.create_campaign("boundary", ["momentum", "quality"])
    return AIResearchInterface(ledger, campaigns)


class TestProposalFlow:
    def test_valid_proposal_accepted_and_reserved(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign(
            "flow", ["momentum"], budget=ResearchBudget(max_trials=2)
        )
        interface = AIResearchInterface(ledger, campaigns)
        result = interface.submit_proposal(
            make_proposal(), campaign_id=campaign.campaign_id
        )
        assert result.verdict == ProposalVerdict.ACCEPTED
        assert result.hypothesis_id == "HYP-00001"
        assert campaigns.require(campaign.campaign_id).trial_count == 1

    def test_schema_rejection_recorded_as_invalid(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("flow", ["momentum"])
        interface = AIResearchInterface(ledger, campaigns)
        result = interface.submit_proposal(
            make_proposal(execute_code="os.system('true')"),
            campaign_id=campaign.campaign_id,
        )
        assert result.verdict == ProposalVerdict.REJECTED_INVALID
        assert "schema rejection" in result.reason
        records = ledger.list_records()
        assert len(records) == 1
        assert records[0].status == "invalid"
        assert "Extra inputs are not permitted" in (records[0].reason or "")

    def test_out_of_bounds_parameters_rejected(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("flow", ["momentum"])
        interface = AIResearchInterface(ledger, campaigns)
        result = interface.submit_proposal(
            make_proposal(parameters={"lookback": 10**9}),
            campaign_id=campaign.campaign_id,
        )
        assert result.verdict == ProposalVerdict.REJECTED_INVALID
        assert "registry rejection" in result.reason

    def test_duplicate_proposal_rejected_and_linked(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign(
            "flow", ["momentum"], budget=ResearchBudget(max_trials=5)
        )
        interface = AIResearchInterface(ledger, campaigns)
        first = interface.submit_proposal(
            make_proposal(), campaign_id=campaign.campaign_id
        )
        assert first.verdict == ProposalVerdict.ACCEPTED
        second = interface.submit_proposal(
            make_proposal(), campaign_id=campaign.campaign_id
        )
        assert second.verdict == ProposalVerdict.REJECTED_DUPLICATE
        assert second.novelty is not None
        assert second.novelty.duplicate_of == first.hypothesis_id
        records = ledger.list_records()
        duplicates = [record for record in records if record.status == "duplicate"]
        assert len(duplicates) == 1
        assert duplicates[0].duplicate_of == first.hypothesis_id

    def test_parameter_variant_is_new_trial(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign(
            "flow", ["momentum"], budget=ResearchBudget(max_trials=5)
        )
        interface = AIResearchInterface(ledger, campaigns)
        first = interface.submit_proposal(
            make_proposal(), campaign_id=campaign.campaign_id
        )
        second = interface.submit_proposal(
            make_proposal(parameters={"lookback": 126}),
            campaign_id=campaign.campaign_id,
        )
        assert first.verdict == ProposalVerdict.ACCEPTED
        assert second.verdict == ProposalVerdict.ACCEPTED
        assert second.hypothesis_id != first.hypothesis_id

    def test_budget_exhaustion_stops_search(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign(
            "tiny", ["momentum"], budget=ResearchBudget(max_trials=1)
        )
        interface = AIResearchInterface(ledger, campaigns)
        first = interface.submit_proposal(
            make_proposal(), campaign_id=campaign.campaign_id
        )
        assert first.verdict == ProposalVerdict.ACCEPTED
        second = interface.submit_proposal(
            make_proposal(parameters={"lookback": 126}),
            campaign_id=campaign.campaign_id,
        )
        assert second.verdict == ProposalVerdict.RESEARCH_BUDGET_EXHAUSTED
        assert "RESEARCH_BUDGET_EXHAUSTED" in second.reason

    def test_ambiguous_family_without_strategy_id_rejected(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("flow", ["momentum"])
        interface = AIResearchInterface(ledger, campaigns)
        result = interface.submit_proposal(
            make_proposal(strategy_id=None), campaign_id=campaign.campaign_id
        )
        assert result.verdict == ProposalVerdict.REJECTED_INVALID

    def test_interface_never_gates_or_measures(self) -> None:
        # The interface's module imports nothing from the measurement or
        # execution stack: no gate, backtest engine, metrics, or broker.
        path = Path("research/ai_research.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module or "")
        assert "research.gate" not in imports
        assert not any("backtest" in name for name in imports)
        assert "ledger" in imports  # records history
        assert "campaign" in imports  # enforces budget


class TestContextBuilder:
    def test_default_context_excludes_results(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("ctx", ["momentum"])
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            strategy_family="momentum",
            metrics={"sharpe": 2.7},
            gate_result={"verdict": "PASS"},
            campaign_id=campaign.campaign_id,
            reason="test",
        )
        context = ResearchContextBuilder(ledger, campaigns).build_context(
            campaign_id=campaign.campaign_id
        )
        assert context["results_included"] is False
        assert len(context["research_history"]) == 1
        entry = context["research_history"][0]
        assert "metrics" not in entry
        assert "gate_result" not in entry
        assert "sharpe" not in str(context)

    def test_results_opt_in(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("ctx", ["momentum"])
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            strategy_family="momentum",
            metrics={"sharpe": 2.7},
            campaign_id=campaign.campaign_id,
            reason="test",
        )
        context = ResearchContextBuilder(ledger, campaigns).build_context(
            campaign_id=campaign.campaign_id, include_results=True
        )
        assert context["results_included"] is True
        assert context["research_history"][0]["metrics"]["sharpe"] == 2.7

    def test_context_reports_failed_families_and_budget(self, tmp_path) -> None:
        ledger = HypothesisLedger(tmp_path / "ledger.jsonl")
        campaigns = CampaignStore(tmp_path / "campaigns.jsonl")
        campaign = campaigns.create_campaign("ctx", ["momentum"])
        ledger.record(
            hypothesis_id="HYP-00001",
            status="rejected",
            strategy="momentum",
            strategy_family="momentum",
            campaign_id=campaign.campaign_id,
            reason="benchmark competitiveness",
        )
        context = ResearchContextBuilder(ledger, campaigns).build_context()
        assert context["failed_families"]["momentum"] == ["benchmark competitiveness"]
        assert context["families_tested"]["momentum"] == 1
        assert context["research_budget"]["consumed"] == 0


class TestHypothesisToExperiment:
    def test_contract_conversion(self) -> None:
        interface = interface_fixture()
        campaign_id = interface.campaigns.list_campaigns()[0].campaign_id
        result = interface.submit_proposal(make_proposal(), campaign_id=campaign_id)
        assert result.hypothesis is not None
        experiment = hypothesis_to_experiment(
            result.hypothesis, universe="fixture", dataset_version="v1"
        )
        assert experiment.hypothesis_id == result.hypothesis_id
        assert experiment.strategy == "momentum"
        assert list(experiment.factor_set) == ["close"]
