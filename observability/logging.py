import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """Structured JSON formatter for production logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # Extract custom context from 'extra'
        if hasattr(record, "symbol"):
            log_obj["symbol"] = record.symbol
        if hasattr(record, "operation"):
            log_obj["operation"] = record.operation
        if hasattr(record, "context"):
            log_obj["context"] = record.context

        if record.exc_info:
            log_obj["error"] = self.formatException(record.exc_info)

        return json.dumps(log_obj)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Get a configured structured logger."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


class ContextLogger:
    """Wrapper to automatically inject context fields into structured logs."""

    def __init__(
        self,
        logger: logging.Logger,
        symbol: Optional[str] = None,
        operation: Optional[str] = None,
    ):
        self.logger = logger
        self.symbol = symbol
        self.operation = operation

    def _log(
        self, level: int, msg: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> None:
        extra = {
            "symbol": self.symbol,
            "operation": self.operation,
            "context": context or {},
        }
        self.logger.log(level, msg, extra=extra, **kwargs)

    def info(self, msg: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, context, **kwargs)

    def error(self, msg: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, context, **kwargs)

    def warning(self, msg: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, context, **kwargs)

    def debug(self, msg: str, context: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, context, **kwargs)
