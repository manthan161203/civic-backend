"""
Logger — console + optional rotating file logger factory
=========================================================
Call ``get_logger(name)`` in each module to get a configured logger.

Configuration comes from settings, which default from ``ENVIRONMENT``:

    LOG_LEVEL    DEBUG in development, INFO in production.
    LOG_FORMAT   "text" in development, "json" in production.
    LOG_TO_FILE  True in development, False in production.
    LOG_DIR      Directory for the rotating log files.

Outputs:
  - Console  : ``LOG_LEVEL`` and above, on stdout.
  - civic_YYYY-MM-DD.log  : ``LOG_LEVEL`` and above, daily rotation, 30 days.
  - errors_YYYY-MM-DD.log : ERROR and above, daily rotation, 30 days.

Two things this module deliberately does *not* do any more:

  - It no longer pins DEBUG in every environment. Debug records include OTPs and
    other credentials; with 30-day file retention and the Sentry logging
    integration forwarding INFO records as breadcrumbs, a debug line outlives
    and outruns whatever it was describing. ``validate_settings()`` rejects
    ``LOG_LEVEL=DEBUG`` in production.

  - It no longer creates the log directory at import time. That ran before any
    application code and blew up with a PermissionError when the working
    directory was not writable by the container user. File logging is now
    opt-in, off by default in production (in a container, stdout *is* the log),
    and a directory that cannot be created degrades to console-only rather than
    taking the process down.
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from app.core.config import settings
from app.core.request_context import RequestIdFilter

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(request_id)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Resolved once: a failed directory creation should warn once, not per logger.
_file_logging_dir: Path | None = None
_file_logging_checked = False


# Attributes every LogRecord carries. Anything on a record that is not in this
# set arrived through the `extra=` kwarg and is application data worth emitting.
# Derived from logging.LogRecord's documented attributes plus the two the stdlib
# adds during formatting.
_STANDARD_RECORD_FIELDS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


class JSONFormatter(logging.Formatter):
    """
    Structured JSON log formatter for production logging.

    Outputs logs as JSON for better parsing, filtering, and monitoring.
    Each log line is valid JSON that can be parsed by log aggregators.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON"""
        log_data: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=None).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
            }

        # Add custom fields passed via the `extra=` kwarg.
        #
        # `logging` sets each key in `extra` as a **top-level attribute** on the
        # record — it never creates a `custom_fields` attribute, and nothing in
        # this codebase passes one. So the previous `hasattr(record,
        # "custom_fields")` check was never true and every structured field the
        # application logged was silently dropped from production JSON output:
        # the admin-override audit trail's `extra={"audit": True}`, the reward
        # ledger's user_id/event_type, the geo-routing diagnostics. The string
        # `"audit"` never appeared in a single log line.
        #
        # The reliable way to recover them is to diff the record's attributes
        # against the standard set.
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            log_data[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)

        return json.dumps(log_data, default=str)


def _resolve_level() -> int:
    """Map ``settings.LOG_LEVEL`` onto a logging constant, defaulting to INFO."""
    return getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)


def _resolve_log_dir() -> Path | None:
    """Return the writable log directory, or None if file logging is unavailable.

    Resolved once per process. A directory that cannot be created is reported
    once on stderr and file logging is skipped — console logging still works.
    """
    global _file_logging_dir, _file_logging_checked

    if _file_logging_checked:
        return _file_logging_dir

    _file_logging_checked = True
    if not settings.LOG_TO_FILE:
        return None

    directory = Path(settings.LOG_DIR)
    if not directory.is_absolute():
        # Relative to the repo root, not the current working directory, so the
        # destination does not change with how the process was launched.
        directory = Path(__file__).resolve().parents[2] / directory

    try:
        directory.mkdir(parents=True, exist_ok=True)
        _file_logging_dir = directory
    except OSError as e:
        # print, not logger: this runs while configuring the logger, so there
        # is no logger to report it with yet.
        print(  # noqa: T201
            f"[logger] File logging disabled — cannot use {directory}: {e}",
            flush=True,
        )
        _file_logging_dir = None

    return _file_logging_dir


def get_logger(name: str, use_json: bool = False) -> logging.Logger:
    """Return a configured logger.

    Args:
        name:     Logger name, conventionally the module or component.
        use_json: Force JSON output regardless of ``settings.LOG_FORMAT``.

    Returns:
        A logger with console and (when enabled) file handlers attached.
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if logger already configured
    if logger.handlers:
        return logger

    level = _resolve_level()
    logger.setLevel(level)

    json_mode = use_json or settings.LOG_FORMAT == "json" or os.getenv("LOG_FORMAT") == "json"
    formatter = JSONFormatter() if json_mode else logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Every record gets the current request ID, so a traceback from a service
    # layer can be matched to the X-Request-ID the client was given.
    request_id_filter = RequestIdFilter()

    # Console handler — the primary sink in a container
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    console.addFilter(request_id_filter)
    logger.addHandler(console)

    log_dir = _resolve_log_dir()
    if log_dir is not None:
        today = datetime.now().strftime("%Y-%m-%d")

        # Daily rotating file handler — everything at the configured level
        file_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, f"civic_{today}.log"),
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(request_id_filter)
        logger.addHandler(file_handler)

        # Error-only rotating file handler
        error_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, f"errors_{today}.log"),
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        logger.addHandler(error_handler)

    logger.propagate = False
    return logger
