"""Structured JSON Logging for production deployments."""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# Context variables for distributed tracing
correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")
execution_id_var: ContextVar[str] = ContextVar("execution_id", default="")


class JSONFormatter(logging.Formatter):
    """Outputs logs in structured JSON format for aggregation (e.g. ELK, Datadog)."""

    def format(self, record: logging.LogRecord) -> str:
        log_record = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "funcName": record.funcName,
            "lineNo": record.lineno,
            "correlation_id": correlation_id_var.get(),
            "execution_id": execution_id_var.get(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        # Add extra fields if they exist
        if hasattr(record, "metadata"):
            log_record["metadata"] = record.metadata  # type: ignore

        return json.dumps(log_record)


def setup_logging(level: int = logging.INFO) -> None:
    """Configures the root logger to output structured JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    root_logger.addHandler(handler)
