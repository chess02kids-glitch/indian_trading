import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from config.env_validator import ConfigurationError, validate_environment
from observability.alerts import AlertService
from observability.health import HealthService, SystemHealth
from reconciliation.engine import ReconciliationEngine, ReconciliationInput


# 1. Environment validation tests
def test_env_validator_valid():
    with patch.dict(os.environ, {"SYSTEM_MODE": "LOCAL"}, clear=True):
        validate_environment()  # Should not raise


def test_env_validator_paper_no_db():
    with patch.dict(os.environ, {"SYSTEM_MODE": "PAPER"}, clear=True):
        with pytest.raises(ConfigurationError):
            validate_environment()


def test_env_validator_live_blocked():
    with patch.dict(os.environ, {"SYSTEM_MODE": "LIVE"}, clear=True):
        with pytest.raises(ConfigurationError):
            validate_environment()


def test_env_validator_credentials_in_paper():
    with patch.dict(
        os.environ,
        {"SYSTEM_MODE": "PAPER", "DATABASE_URL": "db", "UPSTOX_API_KEY": "secret"},
        clear=True,
    ):
        with pytest.raises(ConfigurationError):
            validate_environment()


# 2. AlertService Telegram mockup
def test_alert_service_telegram_mock():
    env = {"TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_CHAT_ID": "chat"}
    service = AlertService(environ=env)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        service.critical("test_event", message="critical failure")

        mock_urlopen.assert_called_once()
        assert len(service.deliveries) == 1
        assert service.deliveries[0]["delivered"] is True


# 3. HealthService transitions
def test_health_service_monotonic(tmp_path):
    status_file = tmp_path / "status.json"
    service = HealthService(status_path=status_file)

    service.set_state(SystemHealth.WARNING, "test warning")
    assert service.state == SystemHealth.WARNING

    # Lower severity should be ignored
    service.set_state(SystemHealth.HEALTHY, "ignored")
    assert service.state == SystemHealth.WARNING

    # Higher severity should apply
    service.set_state(SystemHealth.HALTED, "test halt")
    assert service.state == SystemHealth.HALTED


# 4. Reconciliation Engine integration
def test_reconciliation_engine_alerts_and_locks():
    mock_health = MagicMock()
    mock_alert = MagicMock()
    engine = ReconciliationEngine(health_service=mock_health, alert_service=mock_alert)

    # Create an input that forces a mismatch
    input_data = ReconciliationInput(
        run_id="run-1",
        as_of=datetime.now(timezone.utc),
        expected_positions={"RELIANCE": 10},
        actual_positions=[],
    )

    res = engine.reconcile(input_data)
    assert res.matched is False
    assert res.locked is True

    mock_health.set_state.assert_called_once()
    mock_alert.critical.assert_called_once_with(
        "reconciliation_mismatch",
        message="System LOCKED due to 1 mismatches.",
        run_id="run-1",
    )
