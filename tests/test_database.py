import os
from unittest.mock import MagicMock, patch

import pytest

from config.database import check_connection, get_supabase_client, with_retries


@patch.dict(
    os.environ, {"SUPABASE_URL": "http://localhost", "SUPABASE_KEY": "test-key"}
)
@patch("config.database.create_client")
def test_get_supabase_client(mock_create):
    mock_create.return_value = MagicMock()
    # Reset singleton for testing
    import config.database

    config.database._client_instance = None

    client = get_supabase_client()
    assert client is not None
    mock_create.assert_called_once_with("http://localhost", "test-key")


@patch("config.database.get_supabase_client")
def test_check_connection_success(mock_get_client):
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client

    # Check that it returns True when execute passes
    assert check_connection() is True


@patch("config.database.get_supabase_client")
def test_check_connection_failure(mock_get_client):
    mock_client = MagicMock()
    mock_client.table().select().limit().execute.side_effect = Exception("DB Error")
    mock_get_client.return_value = mock_client

    assert check_connection() is False


def test_with_retries_success():
    mock_func = MagicMock(return_value="success")
    decorated = with_retries(max_retries=3, backoff_factor=0.01)(mock_func)

    result = decorated()
    assert result == "success"
    mock_func.assert_called_once()


def test_with_retries_failure():
    mock_func = MagicMock(side_effect=Exception("Test Error"))
    decorated = with_retries(max_retries=3, backoff_factor=0.01)(mock_func)

    with pytest.raises(Exception, match="Test Error"):
        decorated()

    assert mock_func.call_count == 3
