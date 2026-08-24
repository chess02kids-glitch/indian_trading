"""Tests for Health monitoring."""

from unittest.mock import MagicMock, patch

from auth.health import AuthHealthMonitor


@patch("auth.health.SessionManager")
@patch("auth.health.secrets")
def test_health_monitor_full_diagnostics(mock_secrets, mock_sm_class):
    # Mock startup config
    mock_secrets.verify_startup.return_value = True
    mock_secrets.validate_ip.return_value = True
    mock_secrets._fernet = True

    # Mock SM
    mock_sm = mock_sm_class.return_value
    mock_flow = MagicMock()
    mock_flow.is_configured = True
    mock_sm.get_flow.return_value = mock_flow

    # Mock active DB session
    from datetime import datetime, timedelta, timezone

    future_time = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    mock_sm.repo.get_active_session.return_value = {
        "access_token": "123",
        "expires_at": future_time,
    }

    monitor = AuthHealthMonitor()
    # Mock the connectivity check which uses requests
    monitor._check_broker_connectivity = MagicMock(return_value=True)

    report = monitor.run_full_diagnostics()

    assert report["infrastructure"]["startup_configuration_valid"] is True
    assert report["brokers"]["upstox"]["configured"] is True
    assert report["brokers"]["upstox"]["connectivity_ok"] is True
    assert report["brokers"]["upstox"]["session_active"] is True
    assert report["brokers"]["upstox"]["token_valid"] is True
