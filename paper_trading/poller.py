"""Low-frequency local quote poller for the paper monitor."""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class PaperQuotePoller:
    """Refresh quote snapshots while the virtual paper monitor is running.

    It has no impact on a stopped monitor, and errors remain visible on the
    dashboard through the last recorded snapshot rather than crashing the HTTP
    service.
    """

    def __init__(self, service: Any, interval_seconds: int = 30) -> None:
        self.service = service
        self.interval_seconds = max(10, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="paper-quote-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if self.service.ledger.settings()["running"]:
                    self.service.refresh_quotes()
            except Exception:  # nosec B110 - worker must never take down dashboard
                logger.exception("paper quote poll failed")
