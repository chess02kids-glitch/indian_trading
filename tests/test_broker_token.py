"""Daily token management: expiry tracking, refresh scheduling, manual re-auth."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from broker.errors import BrokerError, StaleTokenError
from broker.token import (
    FileTokenStore,
    TokenManager,
    mask_token,
)
from tests.sandbox_common import T0, FakeClock


def _manager(tmp_path, clock=None):
    return TokenManager(FileTokenStore(tmp_path / "tok"), clock=clock or FakeClock())


class TestRecordAndRetrieve:
    def test_record_then_get(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        issued = manager.record_token("upstox", "tk-123", expires_in_seconds=3600)
        assert manager.get_token("upstox") == "tk-123"
        assert issued.issued_at == T0
        assert issued.expires_at == T0 + timedelta(hours=1)

    def test_empty_token_rejected(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(BrokerError):
            manager.record_token("upstox", "  ", expires_in_seconds=60)

    def test_non_positive_expiry_rejected(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(BrokerError):
            manager.record_token("upstox", "tk-1", expires_in_seconds=0)

    def test_invalid_margin_rejected(self, tmp_path) -> None:
        with pytest.raises(BrokerError):
            TokenManager(FileTokenStore(tmp_path), refresh_margin=timedelta(seconds=0))


class TestExpiryDetection:
    def test_missing_token_raises_stale(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(StaleTokenError, match="no token"):
            manager.get_token("dhan")

    def test_expired_token_raises_stale(self, tmp_path) -> None:
        clock = FakeClock()
        manager = _manager(tmp_path, clock)
        manager.record_token("upstox", "tk-1", expires_in_seconds=3600)
        clock.advance(timedelta(hours=2))
        with pytest.raises(StaleTokenError, match="expired"):
            manager.get_token("upstox")

    def test_status_missing(self, tmp_path) -> None:
        status = _manager(tmp_path).status("upstox")
        assert status.state == "missing"
        assert status.refresh_due

    def test_status_active_then_expiring_soon_then_expired(self, tmp_path) -> None:
        clock = FakeClock()
        manager = _manager(tmp_path, clock)
        manager.record_token("upstox", "tk-1", expires_in_seconds=3600)
        assert manager.status("upstox").state == "active"
        clock.advance(timedelta(minutes=31))  # within 30-minute margin
        assert manager.status("upstox").state == "expiring_soon"
        assert manager.status("upstox").refresh_due
        clock.advance(timedelta(minutes=30))
        assert manager.status("upstox").state == "expired"

    def test_status_masks_token(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        manager.record_token("upstox", "abcdefg", expires_in_seconds=60)
        status = manager.status("upstox")
        assert status.masked_token is not None
        assert "abcdefg" not in status.masked_token

    def test_mask_token_helper(self) -> None:
        assert mask_token(None) is None
        assert mask_token("ab") == "***"
        assert mask_token("abcdefg") == "abc…efg"


class TestRefreshScheduling:
    def test_refresh_due_at(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        manager.record_token("upstox", "tk-1", expires_in_seconds=3600)
        due = manager.refresh_due_at("upstox")
        assert due == T0 + timedelta(minutes=30)

    def test_refresh_due_at_missing(self, tmp_path) -> None:
        assert _manager(tmp_path).refresh_due_at("dhan") is None

    def test_reauth_schedule(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        manager.record_token("upstox", "tk-1", expires_in_seconds=3600)
        schedule = manager.reauth_schedule(["upstox", "dhan"])
        assert schedule["upstox"] == (T0 + timedelta(minutes=30)).isoformat()
        assert schedule["dhan"] is None


class TestManualReauthWorkflow:
    def test_full_manual_flow(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        request = manager.begin_reauth("upstox", "simulated://upstox/oauth?state=x")
        assert request.login_url.startswith("simulated://upstox")
        with pytest.raises(BrokerError, match="state mismatch"):
            manager.complete_reauth(
                "upstox", "wrong-state", "tk-new", expires_in_seconds=60
            )
        record = manager.complete_reauth(
            "upstox", request.state, "tk-new", expires_in_seconds=60
        )
        assert manager.get_token("upstox") == "tk-new"
        assert record.source == "manual-reauth"
        assert manager.pending_reauth("upstox") is None

    def test_complete_without_begin_fails(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(BrokerError, match="no pending"):
            manager.complete_reauth("upstox", "anything", "tk", expires_in_seconds=60)

    def test_begin_requires_url(self, tmp_path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(BrokerError):
            manager.begin_reauth("upstox", "")

    def test_no_automated_login_surface(self, tmp_path) -> None:
        """The framework must not automate login: no method fetches codes."""
        manager = _manager(tmp_path)
        public = [name for name in dir(manager) if not name.startswith("_")]
        forbidden = [n for n in public if "auto" in n.lower() or "browser" in n.lower()]
        assert forbidden == []


class TestFileTokenStore:
    def test_owner_only_permissions(self, tmp_path) -> None:
        store = FileTokenStore(tmp_path / "tok")
        manager = TokenManager(store, clock=FakeClock())
        manager.record_token("upstox", "tk-1", expires_in_seconds=60)
        path = store._path("upstox")
        if os.name == "nt":
            assert path.exists()
        else:
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600

    def test_roundtrip(self, tmp_path) -> None:
        store = FileTokenStore(tmp_path / "tok")
        manager = TokenManager(store, clock=FakeClock())
        manager.record_token("upstox", "tk-1", expires_in_seconds=60)
        reloaded = store.load("upstox")
        assert reloaded is not None and reloaded.access_token == "tk-1"

    def test_malformed_file_raises(self, tmp_path) -> None:
        store = FileTokenStore(tmp_path / "tok")
        path = store._path("upstox")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        with pytest.raises(BrokerError, match="cannot read"):
            store.load("upstox")

    def test_missing_fields_raise(self, tmp_path) -> None:
        store = FileTokenStore(tmp_path / "tok")
        path = store._path("upstox")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"broker": "upstox"}), encoding="utf-8")
        with pytest.raises(BrokerError, match="malformed"):
            store.load("upstox")

    def test_revoke(self, tmp_path) -> None:
        store = FileTokenStore(tmp_path / "tok")
        manager = TokenManager(store, clock=FakeClock())
        manager.record_token("upstox", "tk-1", expires_in_seconds=60)
        manager.revoke("upstox")
        assert store.load("upstox") is None

    def test_broker_name_sanitised(self, tmp_path) -> None:
        store = FileTokenStore(tmp_path / "tok")
        # traversal characters are stripped, so the path can never escape
        path = store._path("../escape")
        assert path.parent == store.directory
        assert path.name == "escape.token.json"
        # a name with no usable characters at all is refused
        with pytest.raises(BrokerError):
            store._path("///")
