"""Tests for the research cockpit API layer.

These tests verify that the dashboard can:
1. List available strategies
2. Check data status
3. Run a full experiment with synthetic data
4. Persist and retrieve experiments
5. Serve the cockpit HTML page
6. Handle API requests correctly
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection

import pytest

from dashboard.cockpit_html import render_cockpit_page
from dashboard.research_api import (
    STRATEGY_CATALOGUE,
    ExperimentResult,
    generate_synthetic_prices,
    get_data_status,
    get_experiment,
    list_experiments,
    list_strategies,
    run_experiment,
)

# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------


class TestListStrategies:
    """Tests for strategy listing."""

    def test_returns_dict(self):
        result = list_strategies()
        assert isinstance(result, dict)

    def test_contains_expected_strategies(self):
        result = list_strategies()
        assert "momentum" in result
        assert "crossover" in result
        assert "mean_reversion" in result

    def test_strategy_has_required_fields(self):
        result = list_strategies()
        for key, strat in result.items():
            assert "label" in strat
            assert "description" in strat
            assert "parameters" in strat
            assert isinstance(strat["parameters"], dict)

    def test_strategy_parameters_have_metadata(self):
        result = list_strategies()
        for key, strat in result.items():
            for p_name, param in strat["parameters"].items():
                assert "type" in param
                assert "default" in param
                assert "label" in param


# ---------------------------------------------------------------------------
# Data status
# ---------------------------------------------------------------------------


class TestGetDataStatus:
    """Tests for data availability checking."""

    def test_returns_dict(self):
        result = get_data_status()
        assert isinstance(result, dict)

    def test_has_prices_file(self):
        result = get_data_status()
        assert "prices_file" in result
        assert "prices_exists" in result

    def test_has_universe_files(self):
        result = get_data_status()
        assert "universe_files" in result
        assert isinstance(result["universe_files"], dict)

    def test_reports_missing_prices(self, tmp_path):
        result = get_data_status(prices_path=tmp_path / "nonexistent.parquet")
        assert result["prices_exists"] is False

    def test_reports_existing_file(self, tmp_path):
        # Create a dummy file
        dummy = tmp_path / "prices.csv"
        dummy.write_text("date,symbol,close\n2020-01-01,AAPL,100\n")
        result = get_data_status(prices_path=dummy)
        assert result["prices_exists"] is True


# ---------------------------------------------------------------------------
# Synthetic data generation
# ---------------------------------------------------------------------------


class TestSyntheticData:
    """Tests for synthetic data generation."""

    def test_generates_market_data(self):
        data = generate_synthetic_prices(n_symbols=5, n_days=100, seed=42)
        assert data.close.shape == (100, 5)

    def test_deterministic(self):
        data1 = generate_synthetic_prices(seed=42)
        data2 = generate_synthetic_prices(seed=42)
        assert data1.close.equals(data2.close)

    def test_different_seeds_differ(self):
        data1 = generate_synthetic_prices(seed=42)
        data2 = generate_synthetic_prices(seed=99)
        assert not data1.close.equals(data2.close)

    def test_positive_prices(self):
        data = generate_synthetic_prices()
        assert (data.close > 0).all().all()


# ---------------------------------------------------------------------------
# Full experiment execution
# ---------------------------------------------------------------------------


class TestRunExperiment:
    """Tests for the full research pipeline."""

    def test_runs_momentum_with_synthetic_data(self):
        result = run_experiment(
            "momentum",
            {"lookback": 21},
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            seed=42,
            tracking_dir="/tmp/test_experiments",
        )
        assert isinstance(result, ExperimentResult)
        assert result.strategy == "momentum"
        assert result.verdict in ("PASS", "FAIL", "FRAGILE", "INSUFFICIENT_EVIDENCE")
        assert 0 <= result.score <= 100
        assert result.metrics is not None
        assert "sharpe" in result.metrics

    def test_runs_crossover_with_synthetic_data(self):
        result = run_experiment(
            "crossover",
            {"fast_window": 10, "slow_window": 30},
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            seed=42,
            tracking_dir="/tmp/test_experiments",
        )
        assert result.strategy == "crossover"
        assert result.verdict in ("PASS", "FAIL", "FRAGILE", "INSUFFICIENT_EVIDENCE")

    def test_runs_mean_reversion_with_synthetic_data(self):
        result = run_experiment(
            "mean_reversion",
            {"window": 20, "entry_zscore": -1.0},
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            seed=42,
            tracking_dir="/tmp/test_experiments",
        )
        assert result.strategy == "mean_reversion"
        assert result.verdict in ("PASS", "FAIL", "FRAGILE", "INSUFFICIENT_EVIDENCE")

    def test_result_has_gate_checks(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        assert len(result.gate_checks) > 0
        for check in result.gate_checks:
            assert "name" in check
            assert "status" in check
            assert "message" in check
            assert check["status"] in ("pass", "warn", "fail")

    def test_result_has_validation(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        assert "fold_metrics" in result.validation
        assert "windows" in result.validation
        assert len(result.validation["fold_metrics"]) > 0

    def test_result_has_benchmarks(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        assert len(result.benchmarks) > 0
        for name, metrics in result.benchmarks.items():
            assert "sharpe" in metrics

    def test_result_has_equity_curve(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        assert result.equity_curve_data is not None
        assert len(result.equity_curve_data) > 0
        assert "date" in result.equity_curve_data[0]
        assert "value" in result.equity_curve_data[0]

    def test_result_has_drawdown(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        assert result.drawdown_data is not None
        assert len(result.drawdown_data) > 0

    def test_failed_experiment_has_reason(self):
        """A FAIL verdict must have a human-readable rejection reason."""
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        if result.verdict != "PASS":
            assert result.rejection_reason is not None
            assert len(result.rejection_reason) > 0

    def test_result_to_dict(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        # Must be JSON serialisable
        json.dumps(d, default=str)

    def test_result_has_consistency(self):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir="/tmp/test_experiments",
        )
        assert "positive_fold_fraction" in result.consistency
        assert "folds" in result.consistency


# ---------------------------------------------------------------------------
# Experiment history
# ---------------------------------------------------------------------------


class TestExperimentHistory:
    """Tests for experiment persistence and retrieval."""

    def test_list_experiments_empty(self, tmp_path):
        result = list_experiments(tracking_dir=tmp_path / "nonexistent")
        assert result == []

    def test_list_experiments_after_run(self, tmp_path):
        run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir=tmp_path,
        )
        experiments = list_experiments(tracking_dir=tmp_path)
        assert len(experiments) >= 1
        assert experiments[0]["strategy"] == "momentum"

    def test_get_experiment_by_run_id(self, tmp_path):
        result = run_experiment(
            "momentum",
            use_synthetic=True,
            train_size=100,
            test_size=50,
            placebo_samples=10,
            tracking_dir=tmp_path,
        )
        # The run_id is stored in hypothesis_id (the local ExperimentManager
        # sets run_id to "local" when no MLflow is available).
        exp = get_experiment(result.run_id, tracking_dir=tmp_path)
        assert exp is not None
        assert exp["hypothesis_id"] == result.run_id

    def test_get_nonexistent_experiment(self, tmp_path):
        exp = get_experiment("nonexistent-id", tracking_dir=tmp_path)
        assert exp is None


# ---------------------------------------------------------------------------
# Cockpit HTML rendering
# ---------------------------------------------------------------------------


class TestCockpitHTML:
    """Tests for the cockpit HTML generation."""

    def test_renders_bytes(self):
        result = render_cockpit_page(STRATEGY_CATALOGUE, get_data_status())
        assert isinstance(result, bytes)

    def test_contains_doctype(self):
        result = render_cockpit_page(STRATEGY_CATALOGUE, get_data_status())
        assert b"<!DOCTYPE html>" in result

    def test_contains_strategy_names(self):
        result = render_cockpit_page(STRATEGY_CATALOGUE, get_data_status())
        html_text = result.decode("utf-8")
        assert "momentum" in html_text
        assert "crossover" in html_text
        assert "mean_reversion" in html_text

    def test_contains_api_endpoints(self):
        result = render_cockpit_page(STRATEGY_CATALOGUE, get_data_status())
        html_text = result.decode("utf-8")
        assert "/api/research/run" in html_text
        assert "/api/research/experiments" in html_text

    def test_contains_tab_structure(self):
        result = render_cockpit_page(STRATEGY_CATALOGUE, get_data_status())
        html_text = result.decode("utf-8")
        assert "panel-data" in html_text
        assert "panel-run" in html_text
        assert "panel-results" in html_text
        assert "panel-history" in html_text


# ---------------------------------------------------------------------------
# Server endpoint tests
# ---------------------------------------------------------------------------


class TestServerEndpoints:
    """Tests for the HTTP server endpoints."""

    @pytest.fixture
    def server(self):
        """Start a test server on a random port."""
        from http.server import ThreadingHTTPServer

        from dashboard.server import DashboardHandler

        server = ThreadingHTTPServer(("127.0.0.1", 0), DashboardHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield port
        server.shutdown()

    def test_healthz(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["status"] == "ok"
        conn.close()

    def test_cockpit_page(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"Research Cockpit" in resp.read()
        conn.close()

    def test_operations_page(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/operations")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"Operations" in resp.read()
        conn.close()

    def test_strategies_api(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/api/strategies")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "momentum" in data
        conn.close()

    def test_data_status_api(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/api/data-status")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "prices_exists" in data
        conn.close()

    def test_experiments_api(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/api/research/experiments")
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert isinstance(data, list)
        conn.close()

    def test_404_for_unknown_path(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request("GET", "/nonexistent")
        resp = conn.getresponse()
        assert resp.status == 404
        conn.close()

    def test_post_research_run_synthetic(self, server):
        payload = json.dumps(
            {
                "strategy": "momentum",
                "parameters": {"lookback": 21},
                "use_synthetic": True,
                "train_size": 100,
                "test_size": 50,
                "placebo_samples": 10,
                "seed": 42,
            }
        ).encode()
        conn = HTTPConnection("127.0.0.1", server, timeout=120)
        conn.request(
            "POST",
            "/api/research/run",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["strategy"] == "momentum"
        assert data["verdict"] in ("PASS", "FAIL", "FRAGILE", "INSUFFICIENT_EVIDENCE")
        assert "metrics" in data
        assert "gate_checks" in data
        conn.close()

    def test_post_research_run_missing_strategy(self, server):
        payload = json.dumps({"parameters": {}}).encode()
        conn = HTTPConnection("127.0.0.1", server)
        conn.request(
            "POST",
            "/api/research/run",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()

    def test_post_research_run_invalid_json(self, server):
        conn = HTTPConnection("127.0.0.1", server)
        conn.request(
            "POST",
            "/api/research/run",
            body=b"not json",
            headers={
                "Content-Type": "application/json",
                "Content-Length": "8",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 400
        conn.close()
