from pathlib import Path
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import List, Dict, Optional
import json
import os
from colorama import init, Fore, Style
import threading

init(autoreset=True)

from managers.config_manager import APP_DIR

# Same place as the config: the project root, or the user data folder when packaged.
BASE_DIR = APP_DIR
LOG_DIR = BASE_DIR / "logs"
HISTORY_DIR = BASE_DIR / "history"
HISTORY_LOG = HISTORY_DIR / "download_history.log"
FAILED_FILE = HISTORY_DIR / "failed_downloads.json"

# Downloads are data, so they go in JSON with the track's metadata alongside.
DOWNLOAD_LOGS = {
    "success": LOG_DIR / "success.json",  # completed downloads
    "failed": LOG_DIR / "failed.json",    # downloads that didn't make it
}

# Diagnostics are for reading, so they stay plain text: message + the link.
TEXT_LOGS = {
    "error": LOG_DIR / "error.log",       # errors during downloading
    "warning": LOG_DIR / "warning.log",   # warnings
    "info": LOG_DIR / "info.log",         # progress notes: saved, resumed, cleaned up
}

TEXT_LEVELS = {
    "error": logging.ERROR,
    "warning": logging.WARNING,
    "info": logging.INFO,
}

LOG_PATHS = {**DOWNLOAD_LOGS, **TEXT_LOGS}

MAX_BYTES = 2_000_000
BACKUP_COUNT = 3
MAX_ENTRIES = 5000        # per JSON log; the older half rolls into <name>.1.json

# yt-dlp metadata keys worth keeping beside a link. Anything else is dropped,
# because a full yt-dlp info dict runs to thousands of lines per track.
METADATA_KEYS = ("title", "artist", "album", "playlist", "playlist_count",
                 "webpage_url", "id", "ext", "filesize", "format")

COLOR_MAP = {
    "success": Fore.GREEN,
    "failed": Fore.RED,
    "error": Fore.YELLOW,
    "warning": Fore.MAGENTA,
    "info": Fore.CYAN,
}

_lock = threading.RLock()     # reentrant: _emit and the JSON writers nest
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
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_LOG.touch(exist_ok=True)

        # Only the text logs need the logging module. The JSON logs are
        # written as records, not lines.
        error_format = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

        for kind, path in TEXT_LOGS.items():
            logger = logging.getLogger(f"downloader.{kind}")
            logger.setLevel(TEXT_LEVELS[kind])
            logger.propagate = False
            if not logger.handlers:          # adding twice would double every line
                handler = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                                              backupCount=BACKUP_COUNT, encoding="utf-8")
                handler.setFormatter(error_format)
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

# ==================== JSON helpers ====================
def _read_json(path: Path, fallback):
    """Load a JSON file. A corrupt or missing file gives the fallback, never raises."""
    try:
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else fallback
    except FileNotFoundError:
        return fallback
    except (OSError, ValueError):
        print(f"{Fore.YELLOW}Could not read {path.name}; starting from empty.{Style.RESET_ALL}")
        return fallback

def _write_json(path: Path, data) -> bool:
    """Write JSON atomically: a crash can't leave a half-written record file."""
    setup()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as error:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"{Fore.RED}Could not write {path.name}: {error}{Style.RESET_ALL}")
        return False

def clean_metadata(metadata: Optional[dict]) -> dict:
    """Keep the handful of metadata fields worth storing beside a link."""
    if not isinstance(metadata, dict):
        return {}
    kept = {}
    for key in METADATA_KEYS:
        value = metadata.get(key)
        if value in (None, "", []):
            continue
        kept[key] = value if isinstance(value, (int, float, bool)) else str(value)[:300]
    return kept

def _append_json(path: Path, entry: dict) -> None:
    """Add one record, rolling the file over once it gets long."""
    setup()
    with _lock:
        entries = _read_json(path, [])
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        if len(entries) > MAX_ENTRIES:
            # Keep the overflow in a single rollover file, then start fresh.
            half = MAX_ENTRIES // 2
            _write_json(path.with_name(path.stem + ".1.json"), entries[:-half])
            entries = entries[-half:]
        _write_json(path, entries)

# ==================== Download records (JSON, with metadata) ====================
def _record_download(kind: str, message: str, url: str, metadata: Optional[dict],
                     item_type: str, console: bool, **extra) -> dict:
    """One record per download: what happened, to which link, and its metadata."""
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"),
             "status": kind,
             "message": message}
    if url:
        entry["url"] = url
    if item_type:
        entry["item_type"] = item_type
    meta = clean_metadata(metadata)
    if meta:
        entry["metadata"] = meta
    entry.update({k: v for k, v in extra.items() if v})

    _append_json(DOWNLOAD_LOGS[kind], entry)
    if console and _console_logger:
        label = f" [{meta['title']}]" if meta.get("title") else ""
        _console_logger.info(f"{COLOR_MAP[kind]}{message}{label}{Style.RESET_ALL}")
    return entry

def log_success(message: str, url: str = "", metadata: Optional[dict] = None,
                item_type: str = "", path: str = "", console: bool = True) -> dict:
    """Record a completed download, with whatever is known about the track."""
    return _record_download("success", message, url, metadata, item_type, console,
                            file=str(path) if path else "")

def log_failure(message: str, url: str = "", metadata: Optional[dict] = None,
                item_type: str = "", error: str = "", console: bool = True) -> dict:
    """Record a download that didn't make it."""
    return _record_download("failed", message, url, metadata, item_type, console,
                            error=error[:300] if error else "")

def read_downloads(kind: str = "success", limit: Optional[int] = None) -> List[dict]:
    """Download records, newest first."""
    entries = _read_json(DOWNLOAD_LOGS.get(kind, Path("")), [])
    if not isinstance(entries, list):
        return []
    if limit:
        entries = entries[-limit:]
    return list(reversed(entries))

# ==================== Diagnostics (plain text) ====================
def _log_text(kind: str, message: str, url: str, console: bool, **kwargs) -> None:
    """A line in error.log, warning.log or info.log: the message, then the link it hit."""
    setup()
    line = f"{message} | {url}" if url else message
    with _lock:
        _loggers[kind].log(TEXT_LEVELS[kind], line, **kwargs)
        if console and _console_logger:
            _console_logger.info(f"{COLOR_MAP[kind]}{message}{Style.RESET_ALL}")

def log_error(message: str, exc_info=False, url: str = "", console: bool = True) -> None:
    """Log an error during the download process, with the link it happened on."""
    _log_text("error", message, url, console, exc_info=exc_info)

def log_warning(message: str, url: str = "", console: bool = True) -> None:
    """Log something recoverable the user should still see (throttling, retries)."""
    _log_text("warning", message, url, console)

def log_info(message: str, url: str = "", console: bool = True) -> None:
    """Log routine progress worth keeping (settings saved, batch resumed, files cleaned up)."""
    _log_text("info", message, url, console)

def read_text_log(kind: str = "error", limit: int = 25) -> List[str]:
    """The last lines of a text log, oldest first."""
    path = TEXT_LOGS.get(kind)
    if not path or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.strip()][-limit:]

# ==================== Failed downloads (kept for retries) ====================
def read_failures() -> Dict[str, dict]:
    """Every failed link on record, as {url: entry}. Never raises."""
    data = _read_json(FAILED_FILE, {})
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}
    return {e["url"]: e for e in items if isinstance(e, dict) and e.get("url")}

def _write_failures(records: Dict[str, dict]) -> bool:
    return _write_json(FAILED_FILE, {
        "updated": datetime.now().isoformat(timespec="seconds"),
        "count": len(records),
        "items": sorted(records.values(), key=lambda e: e.get("last_failed", "")),
    })

def record_failure(url: str, title: str = "", error: str = "", source: str = "",
                   throttled: bool = False, item_type: str = "track",
                   metadata: Optional[dict] = None,
                   kind: str = "download_error") -> dict:
    """
    Put a failed link on record, or bump the one already there, and count it
    towards the end-of-run summary.

    attempt_count accumulates across runs. `throttled` notes that the host was
    refusing traffic rather than the link being bad: a throttled link will very
    likely work later, a dead one never will. Throttled attempts are counted
    separately so a link isn't written off after attempts that were never
    really about it. Metadata is merged rather than replaced, so a later
    attempt that knows less doesn't erase what an earlier one found.

    kind: 'not_found', 'rate_limited' or 'download_error'.
    """
    now = datetime.now().isoformat(timespec="seconds")
    meta = clean_metadata(metadata)
    with _lock:
        records = read_failures()
        entry = records.get(url, {
            "url": url, "title": title, "source": source, "item_type": item_type,
            "attempt_count": 0, "throttled_attempts": 0, "first_failed": now,
            "metadata": {},
        })
        entry["title"] = title or meta.get("title") or entry.get("title", "")
        entry["source"] = source or entry.get("source", "")
        entry["item_type"] = item_type or entry.get("item_type", "track")
        entry["attempt_count"] = int(entry.get("attempt_count", 0)) + 1
        if throttled:
            entry["throttled_attempts"] = int(entry.get("throttled_attempts", 0)) + 1
        entry["throttled"] = bool(throttled)
        entry["last_failed"] = now
        entry["last_error"] = (error or "")[:300]
        if meta:
            entry["metadata"] = {**entry.get("metadata", {}), **meta}
        records[url] = entry
        _write_failures(records)

        session_failed_urls.add(url)
        counted = "rate_limited" if throttled else kind
        session_failure_counts[counted] = session_failure_counts.get(counted, 0) + 1
    return entry

def clear_failures(urls) -> int:
    """Drop links from the failed-downloads record. Returns how many were removed."""
    urls = set(urls)
    if not urls:
        return 0
    with _lock:
        records = read_failures()
        removed = [u for u in urls if records.pop(u, None) is not None]
        if removed:
            _write_failures(records)
    return len(removed)

def reset_session() -> None:
    """Forget this session's failure counts (call at the start of each run)."""
    with _lock:
        session_failed_urls.clear()
        for key in session_failure_counts:
            session_failure_counts[key] = 0

# ==================== Clearing ====================
def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

def clear_log(kind: str) -> bool:
    """
    Empty one log: 'success', 'failed', 'error', 'warning', 'info', 'history'
    or 'retry_queue'. Old rotated copies of it are deleted too.

    Text logs are truncated in place rather than deleted, because their
    handler keeps the file open (and Windows won't delete an open file).
    """
    setup()
    with _lock:
        try:
            if kind in DOWNLOAD_LOGS:
                path = DOWNLOAD_LOGS[kind]
                _unlink_quietly(path.with_name(path.stem + ".1.json"))
                return _write_json(path, [])
            if kind in TEXT_LOGS:
                path = TEXT_LOGS[kind]
                for handler in _loggers[kind].handlers:
                    handler.flush()
                with open(path, "w", encoding="utf-8"):
                    pass
                for number in range(1, BACKUP_COUNT + 1):
                    _unlink_quietly(path.with_name(f"{path.name}.{number}"))
                return True
            if kind == "history":
                with open(HISTORY_LOG, "w", encoding="utf-8"):
                    pass
                return True
            if kind == "retry_queue":
                return _write_failures({})
        except OSError as error:
            print(f"{Fore.RED}Could not clear {kind}: {error}{Style.RESET_ALL}")
            return False
    raise ValueError(f"Unknown log: {kind}")

# ================= History Logger ========================
def add_input(url: str, item_type: str, metadata: Optional[dict] = None) -> None:
    """Record the URL or search term the user gave, and what it turned out to be.

    No status here: the download logs say what came of it. One JSON object per
    line, so appending costs nothing and a killed run loses at most one entry.
    """
    setup()
    entry = {"timestamp": datetime.now().isoformat(timespec="seconds"),
             "url": url,
             "type": item_type}
    meta = clean_metadata(metadata)
    if meta:
        entry["metadata"] = meta
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

def get_history(limit: Optional[int] = None) -> List[Dict]:
    """Most recent entries first."""
    entries = _read_history()
    if limit:
        entries = entries[-limit:]
    return list(reversed(entries))