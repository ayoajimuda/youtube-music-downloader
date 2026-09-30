"""Log manager for the downloader. Handles both logs and download history management """

import shutil
import time
from pathlib import Path
import logging
import datetime
from logging.handlers import RotatingFileHandler
import re
from typing import List, Dict, Optional, Tuple
import json
import sys
import os
from colorama import init, Fore, Style
import threading

init(autoreset=True)

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
HISTORY_LOG = BASE_DIR / "history" / "download_history.log"

LOG_PATHS = {
    "success": LOG_DIR / "success.log", # successful download
    "failed": LOG_DIR / "failed.log", # failed downloads
    "error": LOG_DIR / "error.log", # errors during downloading
    "warning": LOG_DIR / "warning.log" # warnings
}

MAX_BYTES = 2_000_000
BACKUP_COUNT = 3

COLOR_MAP = {
    "success": Fore.GREEN,
    "failed": Fore.RED,
    "error": Fore.YELLOW,
    "warning": Fore.MAGENTA,
}

TYPE_ICONS = {
    "track": "🎵", 
    "album": "💿", 
    "playlist": "📋",  
    "artist": "🎤",  
    "batch": "📄"
    }

_lock = threading.Lock()
_loggers: Dict[str, logging.Logger] = {}
_console_logger: Optional[logging.Logger] = None
_set_up = False

# Per-session record of failures, for the summary at the end of a run.
session_failed_urls = set()
session_failure_counts = {
    "not_found": 0,
    "rate_limited": 0,
    "download_error": 0
    }

# ==================== Logs Setup ====================
def setup(force: bool = False) -> None:
    """Create the directories and every logger. Idempotent."""
    global _console_logger, _set_up
    with _lock:
        if _set_up and not force:
            return
        
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_LOG.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_LOG.touch(exist_ok=True)

        file_format = logging.Formatter( "%(asctime)s - %(levelname)s - %(funcName)s - %(lineno)d - %(message)s")
        error_format = logging.Formatter( "%(asctime)s - %(levelname)s - %(message)s")

        # Levels of logging for different log files
        levels = {
                "success": logging.INFO, 
                "failed": logging.INFO,
                "error": logging.ERROR, 
                "warning": logging.WARNING
                }
        
        # Formats of logging for 
        formats = {
                "success": file_format, 
                "failed": file_format,
                "error": error_format, 
                "warning": error_format
                }

        for kind, path in LOG_PATHS.items():
            logger = logging.getLogger(f"downloader.{kind}")
            logger.setLevel(levels[kind])
            logger.propagate = False
            if not logger.handlers:          # adding twice would double every line
                handler = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                                              backupCount=BACKUP_COUNT, encoding="utf-8")
                handler.setFormatter(formats[kind])
                logger.addHandler(handler)
            _loggers[kind] = logger

        # --- Console logger (terminal) – timestamp‑free ---
        _console_logger = logging.getLogger("downloader.console")
        _console_logger.setLevel(logging.INFO)
        _console_logger.propagate = False
        if not _console_logger.handlers:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter("%(message)s"))  # message only
            _console_logger.addHandler(console_handler)

        _set_up = True
        
# ==================== Logging ====================
def _emit(kind: str, message: str, console: bool, **kwargs) -> None:
    """
    Setups the log

    Args:
        kind (str): _description_
        message (str): _description_
        console (bool): _description_
    """
    setup()
    with _lock:
        logger = _loggers[kind]
        if kind == "error":
            logger.error(message, **kwargs)
        elif kind == "warning":
            logger.warning(message)
        else:
            logger.info(message)
        if console and _console_logger:
            _console_logger.info(f"{COLOR_MAP[kind]}{message}{Style.RESET_ALL}")

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
    """Log something recoverable the user should still see (throttling, retries)."""
    _emit("warning", message, console)


# ==================== Session failures ====================
def record_failure(url: str, kind: str = "download_error") -> None:
    """Note a failed URL for the end-of-run summary.

    kind: 'not_found', 'rate_limited' or 'download_error'.
    """
    with _lock:
        session_failed_urls.add(url)
        session_failure_counts[kind] = session_failure_counts.get(kind, 0) + 1

def session_summary() -> Dict:
    """A snapshot of this session's failures, safe to read from any thread."""
    with _lock:
        return {"urls": set(session_failed_urls),
                "counts": dict(session_failure_counts)}

def reset_session() -> None:
    with _lock:
        session_failed_urls.clear()
        for key in session_failure_counts:
            session_failure_counts[key] = 0
            
# ================= History Logger ========================
def add_input(url: str, item_type: str) -> None:
    """Record the exact URL or search term the user gave. No status: see the logs for that."""
    setup()
    entry = {"timestamp": datetime.now().isoformat(),
             "url": url,
             "type": item_type}
    with _lock:
        try:
            with open(HISTORY_LOG, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass          # history is a convenience; never break a download over it


def _read_history() -> List[Dict]:
    if not HISTORY_LOG.is_file():
        return []
    entries = []
    with _lock:
        try:
            with open(HISTORY_LOG, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue          # a half-written line from a killed run
        except OSError:
            return []
    return entries
