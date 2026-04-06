"""
Logger — Rotating file + console logger factory
================================================
Call ``get_logger(name)`` in each module to get a configured logger.

Outputs:
  - Console  : INFO and above (stdout).
  - civic_YYYY-MM-DD.log : All levels (DEBUG+), rotated daily, 30-day retention.
  - errors_YYYY-MM-DD.log: ERROR+ only, rotated daily, 30-day retention.

Both log files are written to the ``logs/`` directory (auto-created at import time).
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if logger already configured
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Console handler — INFO and above shown in terminal
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
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
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
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
    error_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    logger.addHandler(error_handler)

    logger.propagate = False
    return logger
