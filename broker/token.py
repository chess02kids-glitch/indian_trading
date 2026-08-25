"""Daily broker token management framework.

Indian broker sessions expire daily (e.g. Upstox) or on short cycles. This
module provides the framework for dealing with that lifecycle:

* **expiry tracking** — :class:`TokenRecord` + :meth:`TokenManager.status`
* **expiry detection** — :meth:`TokenManager.get_token` raises
  :class:`StaleTokenError` when the token is missing or expired, and the
  safe-execution layer converts that into a deterministic order rejection
  *before* anything is submitted to a broker.
* **refresh scheduling** — :meth:`TokenManager.refresh_due_at` returns the
  wall-clock moment a human should re-authenticate (a margin before expiry).
* **manual re-auth workflow** — :meth:`TokenManager.begin_reauth` prepares a
  login URL and pending state; :meth:`TokenManager.complete_reauth` stores the
  token obtained from a code that a *human* pasted in.

Login is deliberately **not automated**: no headless browser, no credential
replay, no scheduled auto-login (ADR-009). This module only prepares and
tracks the workflow; a human always performs the actual authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from broker.errors import BrokerError, StaleTokenError

__all__ = [
    "TokenRecord",
    "TokenStatus",
    "TokenStore",
    "FileTokenStore",
    "ReauthRequest",
    "TokenManager",
    "mask_token",
    "default_token_dir",
]

DEFAULT_REFRESH_MARGIN = timedelta(minutes=30)


def default_token_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Default on-disk token directory (inside the configured data dir)."""
    source = os.environ if environ is None else environ
    return Path(source.get("QUANT_DATA_DIR", "data")) / "broker_tokens"


def mask_token(token: str | None) -> str | None:
    """Return a log-safe masked view of a token (never the raw value)."""
    if not token:
        return None
    if len(token) <= 6:
        return "***"
    return f"{token[:3]}…{token[-3:]}"


@dataclass(frozen=True)
class TokenRecord:
    """One stored broker token with its expiry metadata."""

    broker: str
    access_token: str
    issued_at: datetime
    expires_at: datetime
    source: str = "manual-oauth"

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def seconds_until_expiry(self, now: datetime) -> float:
        return (self.expires_at - now).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "access_token": self.access_token,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "source": self.source,
        }

    @staticmethod
    def from_dict(payload: Mapping[str, Any]) -> "TokenRecord":
        try:
            return TokenRecord(
                broker=str(payload["broker"]),
                access_token=str(payload["access_token"]),
                issued_at=datetime.fromisoformat(str(payload["issued_at"])),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])),
                source=str(payload.get("source", "manual-oauth")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BrokerError(f"stored token record is malformed: {exc}") from exc


@dataclass(frozen=True)
class TokenStatus:
    """Point-in-time view of one broker's token health."""

    broker: str
    state: str  # "active" | "expiring_soon" | "expired" | "missing"
    expires_in_seconds: float | None
    refresh_due: bool
    masked_token: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "state": self.state,
            "expires_in_seconds": self.expires_in_seconds,
            "refresh_due": self.refresh_due,
            "masked_token": self.masked_token,
        }


@dataclass(frozen=True)
class ReauthRequest:
    """Prepared manual re-authentication request.

    Carries everything a human needs to log in by hand. ``state`` binds the
    eventual code exchange to this specific request (replay protection).
    """

    broker: str
    login_url: str
    state: str


@runtime_checkable
class TokenStore(Protocol):
    """Persistence for token records."""

    def load(self, broker: str) -> TokenRecord | None: ...
    def save(self, record: TokenRecord) -> None: ...
    def delete(self, broker: str) -> None: ...


class FileTokenStore:
    """JSON token store with owner-only permissions and atomic writes."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._directory = (
            Path(directory) if directory is not None else default_token_dir()
        )

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, broker: str) -> Path:
        safe = "".join(ch for ch in broker.lower() if ch.isalnum() or ch in "-_")
        if not safe:
            raise BrokerError("broker name must be alphanumeric")
        return self._directory / f"{safe}.token.json"

    def load(self, broker: str) -> TokenRecord | None:
        path = self._path(broker)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrokerError(f"cannot read stored token for {broker}: {exc}") from exc
        return TokenRecord.from_dict(payload)

    def save(self, record: TokenRecord) -> None:
        path = self._path(record.broker)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record.to_dict(), handle, indent=2, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def delete(self, broker: str) -> None:
        path = self._path(broker)
        if path.exists():
            path.unlink()


class TokenManager:
    """Tracks token expiry and prepares (never automates) re-authentication.

    Parameters
    ----------
    store:
        Persistence backend (defaults to :class:`FileTokenStore`).
    clock:
        Injectable time source so expiry logic is deterministic under test.
    refresh_margin:
        How long before expiry the token is considered due for refresh.
    """

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        refresh_margin: timedelta = DEFAULT_REFRESH_MARGIN,
    ) -> None:
        if refresh_margin <= timedelta(0):
            raise BrokerError("refresh_margin must be positive")
        self._store = store if store is not None else FileTokenStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._refresh_margin = refresh_margin
        self._pending: dict[str, ReauthRequest] = {}

    # -- queries ---------------------------------------------------------

    def now(self) -> datetime:
        return self._clock()

    def get_record(self, broker: str) -> TokenRecord | None:
        return self._store.load(broker)

    def status(self, broker: str, *, now: datetime | None = None) -> TokenStatus:
        """Expiry-tracked status of one broker's token."""
        moment = now or self._clock()
        record = self._store.load(broker)
        if record is None:
            return TokenStatus(broker, "missing", None, True, None)
        seconds_left = record.seconds_until_expiry(moment)
        if record.is_expired(moment):
            return TokenStatus(
                broker, "expired", seconds_left, True, mask_token(record.access_token)
            )
        refresh_at = record.expires_at - self._refresh_margin
        due = moment >= refresh_at
        state = "expiring_soon" if due else "active"
        return TokenStatus(
            broker, state, seconds_left, due, mask_token(record.access_token)
        )

    def refresh_due_at(self, broker: str) -> datetime | None:
        """Wall-clock moment a human should re-authenticate (expiry - margin)."""
        record = self._store.load(broker)
        if record is None:
            return None
        return record.expires_at - self._refresh_margin

    def reauth_schedule(self, brokers: list[str]) -> dict[str, str | None]:
        """ISO schedule of recommended re-auth moments for several brokers."""
        schedule: dict[str, str | None] = {}
        for broker in brokers:
            due = self.refresh_due_at(broker)
            schedule[broker] = due.isoformat() if due is not None else None
        return schedule

    def get_token(self, broker: str, *, now: datetime | None = None) -> str:
        """Return a valid token or raise :class:`StaleTokenError`.

        This is the client-side expiry-detection gate: callers must check
        *before* submitting anything to a broker so a stale token can never
        reach an order endpoint.
        """
        moment = now or self._clock()
        record = self._store.load(broker)
        if record is None:
            raise StaleTokenError(
                f"no token stored for {broker}; run 'broker login {broker}' first"
            )
        if record.is_expired(moment):
            raise StaleTokenError(
                f"token for {broker} expired at {record.expires_at.isoformat()}; "
                f"manual re-authentication required ('broker login {broker}')"
            )
        return record.access_token

    # -- lifecycle ---------------------------------------------------------

    def record_token(
        self,
        broker: str,
        access_token: str,
        *,
        issued_at: datetime | None = None,
        expires_in_seconds: float,
        source: str = "manual-oauth",
    ) -> TokenRecord:
        """Store a freshly obtained token with its expiry metadata."""
        if not isinstance(access_token, str) or not access_token.strip():
            raise BrokerError("access token must be a non-empty string")
        if expires_in_seconds <= 0:
            raise BrokerError("expires_in_seconds must be positive")
        start = issued_at or self._clock()
        record = TokenRecord(
            broker=broker,
            access_token=access_token,
            issued_at=start,
            expires_at=start + timedelta(seconds=float(expires_in_seconds)),
            source=source,
        )
        self._store.save(record)
        return record

    def revoke(self, broker: str) -> None:
        """Delete the stored token (logout)."""
        self._store.delete(broker)

    # -- manual re-auth workflow --------------------------------------------

    def begin_reauth(self, broker: str, login_url: str) -> ReauthRequest:
        """Prepare a manual re-authentication request for one broker.

        A random-but-derived state token binds the request; completion is
        possible only with the code a human obtains after visiting the URL.
        Nothing here performs or schedules the login itself.
        """
        if not isinstance(login_url, str) or not login_url.strip():
            raise BrokerError("login_url must be a non-empty string")
        seed = f"{broker}|{login_url}|{self._clock().isoformat()}"
        state = "reauth-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        request = ReauthRequest(broker=broker, login_url=login_url, state=state)
        self._pending[broker] = request
        return request

    def complete_reauth(
        self,
        broker: str,
        state: str,
        access_token: str,
        *,
        expires_in_seconds: float,
        issued_at: datetime | None = None,
    ) -> TokenRecord:
        """Finish a manual re-auth started by :meth:`begin_reauth`.

        ``state`` must match the pending request. This method never *fetches*
        a token: the caller supplies the token obtained from the code the
        human pasted after logging in with the broker.
        """
        pending = self._pending.get(broker)
        if pending is None:
            raise BrokerError(
                f"no pending re-authentication for {broker}; call begin_reauth first"
            )
        if not hmac.compare_digest(pending.state, str(state)):
            raise BrokerError("re-authentication state mismatch; start over")
        record = self.record_token(
            broker,
            access_token,
            issued_at=issued_at,
            expires_in_seconds=expires_in_seconds,
            source="manual-reauth",
        )
        del self._pending[broker]
        return record

    def pending_reauth(self, broker: str) -> ReauthRequest | None:
        return self._pending.get(broker)
