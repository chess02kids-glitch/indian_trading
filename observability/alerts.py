"""Alert service with severity levels and a Telegram transport boundary.

All alerting goes through this single service — no module elsewhere in the
system contains Telegram logic. Credentials are read from the environment
(``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``) and never stored in code.
When credentials are absent, alerts are still recorded locally (in-memory +
structured log) so paper runs remain fully observable.
"""

from __future__ import annotations

import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Mapping

from .logging import get_logger

__all__ = ["Alert", "AlertService", "AlertSeverity"]

_ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
_ENV_CHAT_ID = "TELEGRAM_CHAT_ID"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Alert:
    """One alert event."""

    severity: AlertSeverity
    event: str
    message: str
    timestamp: datetime
    context: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "event": self.event,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "context": dict(self.context),
        }


class AlertService:
    """Records alerts locally and forwards them to Telegram when configured."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 5.0,
        max_local_alerts: int = 1000,
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._timeout = float(timeout_seconds)
        self._max_local = int(max_local_alerts)
        self._alerts: list[Alert] = []
        self._lock = threading.Lock()
        self.logger = get_logger("quant_india.alerts")
        self.deliveries: list[dict[str, Any]] = []

    # -- config --------------------------------------------------------------

    def telegram_configured(self) -> bool:
        token = self._environ.get(_ENV_TOKEN, "").strip()
        chat_id = self._environ.get(_ENV_CHAT_ID, "").strip()
        return bool(token) and bool(chat_id)

    # -- API -----------------------------------------------------------------

    def alert(
        self,
        severity: AlertSeverity | str,
        event: str,
        message: str | None = None,
        **context: Any,
    ) -> Alert:
        """Record (and optionally deliver) one alert. Never raises."""
        normalized = (
            severity
            if isinstance(severity, AlertSeverity)
            else AlertSeverity(str(severity).upper())
        )
        alert = Alert(
            severity=normalized,
            event=str(event),
            message=str(message or event),
            timestamp=datetime.now(UTC),
            context=dict(context),
        )
        with self._lock:
            self._alerts.append(alert)
            if len(self._alerts) > self._max_local:
                del self._alerts[: len(self._alerts) - self._max_local]
        self.logger.log(
            self._level_for(normalized),
            alert.event,
            extra={
                "severity": normalized.value,
                "operation": "alert",
                "context": alert.context,
            },
        )
        if normalized is not AlertSeverity.INFO and self.telegram_configured():
            self._deliver_telegram(alert)
        return alert

    def info(self, event: str, **context: Any) -> Alert:
        return self.alert(AlertSeverity.INFO, event, **context)

    def warning(self, event: str, **context: Any) -> Alert:
        return self.alert(AlertSeverity.WARNING, event, **context)

    def critical(self, event: str, **context: Any) -> Alert:
        return self.alert(AlertSeverity.CRITICAL, event, **context)

    def list_alerts(self) -> tuple[Alert, ...]:
        with self._lock:
            return tuple(self._alerts)

    # -- transport -------------------------------------------------------------

    @staticmethod
    def _level_for(severity: AlertSeverity) -> int:
        import logging

        return {
            AlertSeverity.INFO: logging.INFO,
            AlertSeverity.WARNING: logging.WARNING,
            AlertSeverity.CRITICAL: logging.ERROR,
        }[severity]

    def _deliver_telegram(self, alert: Alert) -> None:
        token = self._environ.get(_ENV_TOKEN, "").strip()
        chat_id = self._environ.get(_ENV_CHAT_ID, "").strip()
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode(
            {
                "chat_id": chat_id,
                "text": f"[{alert.severity.value}] {alert.event}: {alert.message}",
            }
        ).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                ok = 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Delivery failure is logged, never fatal: the local record exists.
            self.deliveries.append(
                {"event": alert.event, "delivered": False, "error": str(exc)}
            )
            self.logger.error(
                "telegram_delivery_failed",
                extra={"operation": "alert", "context": {"event": alert.event}},
            )
            return
        self.deliveries.append({"event": alert.event, "delivered": ok})
