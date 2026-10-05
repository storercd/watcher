"""
Shared logging setup for Watcher.

Call :func:`configure_logging` once at startup (the GUI entrypoint does
this). Every module then does the usual ``logging.getLogger(__name__)`` and
inherits console + rotating-file handlers, so diagnosing "is the app
actually hanging, and where" doesn't require re-running under a debugger.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_DIR = Path(os.path.expanduser("~/.watcher"))
DEFAULT_LOG_PATH = DEFAULT_LOG_DIR / "watcher.log"

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def configure_logging(level: int = logging.DEBUG, path: Path = DEFAULT_LOG_PATH) -> None:
    """
    Configure the root "watcher" logger with console + rotating-file handlers.

    Safe to call more than once; only the first call takes effect (detected
    by checking whether the logger already has handlers attached).
    """
    logger = logging.getLogger("watcher")
    if logger.handlers:
        return
    logger.setLevel(level)

    path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
