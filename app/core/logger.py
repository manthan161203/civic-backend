"""
Logger — Rotating file + console logger factory
================================================
Call ``get_logger(name)`` in each module to get a configured logger.

Outputs:
  - Console  : INFO and above (stdout).
  - civic_YYYY-MM-DD.log : All levels (DEBUG+), rotated daily, 30-day retention.
  - errors_YYYY-MM-DD.log: ERROR+ only, rotated daily, 30-day retention.

Both log files are written to the ``logs/`` directory (auto-created at import time).

LOW PRIORITY BUG FIX #1: Structured logging with JSON format for better parsing and monitoring.
"""

import logging
import os
import json
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from typing import Any, Dict

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


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
        
        # Add custom fields if present (via extra parameter)
        if hasattr(record, "custom_fields"):
            log_data.update(record.custom_fields)
        
        return json.dumps(log_data)


def get_logger(name: str, use_json: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if logger already configured
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    
    # Check environment variable for JSON logging
    json_mode = use_json or os.getenv("LOG_FORMAT") == "json"
    formatter = JSONFormatter() if json_mode else logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Console handler — INFO and above shown in terminal
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # Daily rotating file handler — all levels
    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, f"civic_{today}.log"),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Error-only rotating file handler
    error_handler = TimedRotatingFileHandler(
        filename=os.path.join(LOG_DIR, f"errors_{today}.log"),
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
