"""Tests for OAuth flows and session management."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from auth.oauth import UpstoxOAuth
from auth.secrets import BrokerCredentials
from auth.session import SessionManager


@pytest.fixture
def mock_upstox_flow():
    creds = BrokerCredentials("key123", "secret123", "http://localhost")
    flow = UpstoxOAuth()
    flow.credentials = creds
    return flow


def test_upstox_generate_login_url(mock_upstox_flow):
    url = mock_upstox_flow.generate_login_url(state="test_state")
    assert "api.upstox.com/v2/login/authorization/dialog" in url
    assert "client_id=key123" in url
    assert "state=test_state" in url


def test_upstox_trade_code(mock_upstox_flow):
    token = mock_upstox_flow.trade_code_for_token("auth_code_123")
    assert "access_token" in token
    assert "refresh_token" in token
    assert token["expires_in"] == 86400


@patch("auth.session.APISessionsRepository")
@patch("auth.session.secrets")
def test_session_manager_login(mock_secrets, mock_repo_class):
    mock_repo = mock_repo_class.return_value

    manager = SessionManager()

    # Mock the flow inside the manager
    mock_flow = MagicMock()
    mock_flow.trade_code_for_token.return_value = {
        "access_token": "access123",
        "refresh_token": "refresh123",
        "expires_in": 3600,
    }
    manager.flows["test_broker"] = mock_flow

    manager.login("test_broker", "code123")

    # Validate token was saved to DB
    mock_repo.save_session.assert_called_once()
    args, kwargs = mock_repo.save_session.call_args
    assert args[0] == "system_user"
    assert args[1] == "test_broker"
    assert args[2] == "access123"

    # Validate saved to local secrets
    mock_secrets.save_secure_token.assert_called_once_with(
        "test_broker", mock_flow.trade_code_for_token.return_value
    )


@patch("auth.session.APISessionsRepository")
@patch("auth.session.secrets")
def test_session_manager_get_valid_session_expiry_handling(
    mock_secrets, mock_repo_class
):
    mock_repo = mock_repo_class.return_value
    manager = SessionManager()

    # Set up expired session in DB
    past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    mock_repo.get_active_session.return_value = {
        "access_token": "old_access",
        "refresh_token": "old_refresh",
        "expires_at": past_time,
    }

    # Mock renewal flow
    mock_flow = MagicMock()
    mock_flow.refresh_token.return_value = {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "expires_in": 3600,
    }
    manager.flows["test_broker"] = mock_flow

    token = manager.get_valid_session("test_broker")

    assert token["access_token"] == "new_access"
    mock_flow.refresh_token.assert_called_once_with("old_refresh")
    mock_repo.save_session.assert_called_once()
