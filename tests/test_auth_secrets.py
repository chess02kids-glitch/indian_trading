"""Tests for secrets management and encrypted storage."""

import pytest

from auth.secrets import AuthConfiguration, SecretManager


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("QUANT_ENCRYPTION_KEY", "test_encryption_key_12345")
    monkeypatch.setenv("QUANT_WHITELISTED_IPS", "127.0.0.1, 192.168.1.1")
    monkeypatch.setenv("UPSTOX_API_KEY", "upstox_key")
    monkeypatch.setenv("UPSTOX_API_SECRET", "upstox_secret")
    monkeypatch.setenv("DHAN_CLIENT_ID", "dhan_key")
    monkeypatch.setenv("DHAN_API_SECRET", "dhan_secret")


def test_secret_manager_loads_environment(mock_env):
    """Test that environment variables correctly parse into typed config."""
    manager = SecretManager()

    assert manager.config.upstox.api_key == "upstox_key"
    assert manager.config.dhan.api_secret == "dhan_secret"
    assert "127.0.0.1" in manager.config.whitelisted_ips
    assert manager.config.encryption_key_base == "test_encryption_key_12345"


def test_verify_startup(mock_env):
    """Test startup validation."""
    manager = SecretManager()
    assert manager.verify_startup() is True

    manager.config.encryption_key_base = ""
    assert manager.verify_startup() is False


def test_validate_ip(mock_env):
    """Test IP whitelisting."""
    manager = SecretManager()
    assert manager.validate_ip("127.0.0.1") is True
    assert manager.validate_ip("8.8.8.8") is False


def test_encrypted_storage(mock_env, tmp_path):
    """Test that tokens are encrypted at rest and decrypted successfully."""
    # Override storage dir for test
    config = AuthConfiguration(
        encryption_key_base="test_encryption_key", storage_dir=tmp_path
    )
    manager = SecretManager(config=config)

    payload = {"access_token": "secret_abc", "refresh_token": "secret_xyz"}
    manager.save_secure_token("test_broker", payload)

    # Verify file exists
    filepath = tmp_path / "test_broker_session.dat"
    assert filepath.exists()

    # Verify file is not plain text
    raw_data = filepath.read_text()
    assert "secret_abc" not in raw_data

    # Load and decrypt
    loaded = manager.load_secure_token("test_broker")
    assert loaded == payload


def test_unencrypted_fallback(tmp_path):
    """Test behavior if encryption key is missing (logs warning, stores unencrypted)."""
    config = AuthConfiguration(encryption_key_base="", storage_dir=tmp_path)
    manager = SecretManager(config=config)
    assert manager._fernet is None

    payload = {"access_token": "plain_abc"}
    manager.save_secure_token("test_broker", payload)

    # Verify file is plain JSON
    raw_data = filepath = tmp_path / "test_broker_session.dat"
    assert "plain_abc" in raw_data.read_text()

    loaded = manager.load_secure_token("test_broker")
    assert loaded == payload
