"""Strategy registry tests: bounded, code-defined strategy selection."""

from __future__ import annotations

import pytest

from research.registry import (
    STRATEGY_REGISTRY,
    ParameterOutOfBoundsError,
    StrategyRegistry,
    UnknownStrategyError,
)


@pytest.fixture
def registry() -> StrategyRegistry:
    return StrategyRegistry()


class TestRegistryContents:
    def test_core_families_registered(self, registry) -> None:
        assert {"momentum", "trend", "mean_reversion", "quality", "volatility"} <= set(
            registry.families()
        )

    def test_registry_is_code_defined_and_immutable(self) -> None:
        with pytest.raises(TypeError):
            STRATEGY_REGISTRY["evil"] = object()  # type: ignore[index]

    def test_context_is_ai_facing(self, registry) -> None:
        context = registry.context()
        assert "registered_strategies" in context
        by_id = {
            entry["registry_id"]: entry for entry in context["registered_strategies"]
        }
        assert by_id["momentum"]["family"] == "momentum"
        assert by_id["momentum"]["canonical_parameters"]["lookback"] == 63


class TestRegistryBuild:
    def test_build_momentum(self, registry) -> None:
        strategy = registry.build("momentum")
        assert strategy.name == "momentum"
        assert strategy.parameters["lookback"] == 63

    def test_build_with_valid_parameters(self, registry) -> None:
        strategy = registry.build("momentum", {"lookback": 126})
        assert strategy.parameters["lookback"] == 126

    def test_unknown_strategy_rejected(self, registry) -> None:
        with pytest.raises(UnknownStrategyError):
            registry.build("llm_generated_strategy")

    def test_out_of_bounds_parameter_rejected(self, registry) -> None:
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("momentum", {"lookback": 1_000_000})

    def test_negative_lookback_rejected(self, registry) -> None:
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("momentum", {"lookback": -5})

    def test_unknown_parameter_rejected(self, registry) -> None:
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("momentum", {"momentum_alpha": 0.9})

    def test_non_numeric_parameter_rejected(self, registry) -> None:
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("momentum", {"lookback": "sixty-three"})

    def test_float_for_int_parameter_rejected(self, registry) -> None:
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("momentum", {"lookback": 63.5})

    def test_crossover_method_passthrough(self, registry) -> None:
        strategy = registry.build("crossover", {"method": "ema"})
        assert strategy.parameters["method"] == "ema"

    def test_quantile_bounds(self, registry) -> None:
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("cross_sectional_momentum", {"quantile": 0.99})
        with pytest.raises(ParameterOutOfBoundsError):
            registry.build("cross_sectional_momentum", {"quantile": 0.0})

    def test_frozen_baseline_canonical(self, registry) -> None:
        strategy = registry.build("momentum_quality")
        parameters = strategy.parameters
        assert parameters["momentum_lookback"] == 63
        assert parameters["momentum_quantile"] == 0.25
        assert parameters["quality_quantile"] == 0.5

    def test_validate_proposal_dry_run(self, registry) -> None:
        registry.validate_proposal("momentum", {"lookback": 90})
        with pytest.raises(ParameterOutOfBoundsError):
            registry.validate_proposal("momentum", {"lookback": 999})

    def test_every_registered_strategy_builds(self, registry) -> None:
        for registry_id in registry.ids():
            strategy = registry.build(registry_id)
            assert strategy.name
            assert strategy.parameters
