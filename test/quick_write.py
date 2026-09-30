"""Logging for the downloader: file logs per category plus a coloured console."""

import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from colorama import Fore, Style, init

init(autoreset=True)

LOG_DIR = Path(__file__).resolve().parent / "logs"
MAX_BYTES = 2_000_000
BACKUP_COUNT = 3

COLORS = {
    "success": Fore.GREEN,
    "failed": Fore.RED,
    "error": Fore.YELLOW,
    "warning": Fore.MAGENTA,
}

_lock = threading.Lock()
_loggers = {}
_console = None
_set_up = False

# Per-session record of failures, for a summary at the end of a run.
session_failed_urls = set()
session_failure_counts = {"not_found": 0, "rate_limited": 0, "download_error": 0}


def _make_logger(name: str, filename: str, level: int, fmt: logging.Formatter):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:                       # safe to call setup twice
        handler = RotatingFileHandler(LOG_DIR / filename, maxBytes=MAX_BYTES,
                                      backupCount=BACKUP_COUNT, encoding="utf-8")
        handler.setLevel(level)
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def setup_logs() -> None:
    """Create the log directory, file loggers and console logger. Idempotent."""
    global _console, _set_up
    with _lock:
        if _set_up:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        file_fmt = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s")
        short_fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        _loggers["success"] = _make_logger("dl_success", "success.log", logging.INFO, file_fmt)
        _loggers["failed"] = _make_logger("dl_failed", "failed.log", logging.INFO, file_fmt)
        _loggers["error"] = _make_logger("dl_error", "error.log", logging.ERROR, short_fmt)
        _loggers["warning"] = _make_logger("dl_warning", "warning.log", logging.WARNING, short_fmt)

        _console = logging.getLogger("dl_console")
        _console.setLevel(logging.INFO)
        _console.propagate = False
        if not _console.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            _console.addHandler(handler)
        _set_up = True


def _emit(kind: str, message: str, console: bool, **kwargs) -> None:
    setup_logs()
    with _lock:
        logger = _loggers[kind]
        if kind == "error":
            logger.error(message, **kwargs)
        elif kind == "warning":
            logger.warning(message)
        else:
            logger.info(message)
        if console:
            _console.info(f"{COLORS[kind]}{message}{Style.RESET_ALL}")


def log_success(message: str, console: bool = True) -> None:
    """Log a successful download."""
    _emit("success", message, console)


def log_failure(message: str, console: bool = True) -> None:
    """Log a failed download."""
    _emit("failed", message, console)


def log_error(message: str, exc_info=False, console: bool = True) -> None:
    """Log an error during the download process."""
    _emit("error", message, console, exc_info=exc_info)


def log_warning(message: str, console: bool = True) -> None:
    """Log a warning."""
    _emit("warning", message, console)


def record_failure(url: str, kind: str = "download_error") -> None:
    """Note a failed URL for the end-of-run summary. kind: not_found, rate_limited, download_error."""
    with _lock:
        session_failed_urls.add(url)
        session_failure_counts[kind] = session_failure_counts.get(kind, 0) + 1