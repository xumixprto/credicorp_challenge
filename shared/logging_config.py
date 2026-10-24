"""JSON logs with UTC timestamps, request context, and bounded disk usage."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from threading import Lock

from shared.settings import LoggingSettings

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
_configuration_lock = Lock()
_context_fields = (
    "request_id",
    "session_id",
    "function_name",
    "method",
    "path",
    "status_code",
    "duration_ms",
    "query_length",
    "error_type",
)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _context_fields:
            value = getattr(record, field, None)
            if field == "request_id" and value is None:
                value = request_id_context.get()
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, settings: LoggingSettings) -> logging.Logger:
    """Configure only our logger; Streamlit reruns never add duplicate handlers."""
    logger = logging.getLogger(f"credicorp.{service}")
    with _configuration_lock:
        logger.setLevel(settings.log_level)
        logger.propagate = False
        if logger.handlers:
            return logger

        settings.resolved_log_dir.mkdir(parents=True, exist_ok=True)
        formatter = JsonFormatter(service)
        console = logging.StreamHandler()
        file_handler = RotatingFileHandler(
            settings.resolved_log_dir / f"{service}.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        for handler in (console, file_handler):
            handler.setFormatter(formatter)
            logger.addHandler(handler)
    return logger
