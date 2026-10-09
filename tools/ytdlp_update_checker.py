"""
yt-dlp update checker module.
Checks if a newer version of yt-dlp is available and tells the user how to update it.

Usage:
    info = check_ytdlp_updates()
    notify_update_available(info)
    check_on_startup()            # the same, at most once a day, silent unless there's an update
"""

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from managers import log_manager
from menu.colorful_menu import Enhanced_Menu
from tools.dependency_check import find_program, run_program

PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"
CHECK_EVERY_HOURS = 24


def get_installed_version() -> Optional[str]:
    """
    Get the installed version of yt-dlp (the one set in Settings, or on PATH).

    Returns:
        Version string (e.g., "2024.01.01") or None if yt-dlp is not installed
    """
    path, _ = find_program("yt-dlp")
    if not path:
        return None
    # The standalone .exe unpacks itself on first run, which can take a while.
    code, output = run_program([path, "--version"], timeout=20)
    if code != 0:
        return None
    lines = output.strip().splitlines()
    return lines[0].strip() if lines else None


def get_latest_version(timeout: float = 5) -> Optional[str]:
    """
    Fetch the latest version of yt-dlp from PyPI.

    Returns:
        Latest version string or None if unable to fetch
    """
    request = urllib.request.Request(PYPI_URL, headers={"User-Agent": "youtube-music-downloader"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return str(json.loads(response.read().decode("utf-8"))["info"]["version"])
    except (OSError, ValueError, KeyError):       # URLError/timeouts are OSErrors; bad JSON is ValueError
        return None


def parse_version(version_str: str) -> Tuple[int, ...]:
    """
    Parse a version string to a tuple for comparison.
    "2024.01.01" and "2024.1.1" compare equal; nightly builds keep their 4th part.
    """
    return tuple(int(part) for part in re.findall(r"\d+", version_str or ""))


def is_update_available(current: str, latest: str) -> bool:
    """True if `latest` is newer than `current`. False if either is unknown."""
    if not current or not latest:
        return False
    current_tuple, latest_tuple = parse_version(current), parse_version(latest)
    if not current_tuple or not latest_tuple:
        return False
    return latest_tuple > current_tuple


def update_command(path: Optional[str]) -> str:
    """The update command that matches how yt-dlp was installed."""
    pip = f"{Path(sys.executable).name} -m pip install --upgrade yt-dlp"
    if not path:
        return pip
    low = path.replace("\\", "/").lower()
    if "winget" in low:
        return "winget upgrade yt-dlp.yt-dlp"
    if "/scoop/" in low:
        return "scoop update yt-dlp"
    if "/homebrew/" in low or "/cellar/" in low or "/linuxbrew/" in low:
        return "brew upgrade yt-dlp"
    if "/pipx/" in low:
        return "pipx upgrade yt-dlp"
    if "/scripts/" in low or ("/bin/" in low and _is_script(path)):
        return pip                                   # pip's launcher
    if low.endswith(".exe") or not _is_script(path):
        return f'"{path}" -U'                        # standalone release: updates itself
    return pip


def _is_script(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(2) == b"#!"
    except OSError:
        return False


def check_ytdlp_updates() -> dict:
    """
    Check if yt-dlp has updates available.

    Returns:
        Dict with keys:
        - 'update_available': bool
        - 'current_version': str or None
        - 'latest_version': str or None
        - 'update_command': str (how to update or install it)
        - 'message': str (human-readable message)
    """
    path, where = find_program("yt-dlp")
    current_version = get_installed_version()
    command = update_command(path)

    if not current_version:
        reason = where if not path else "it's there but didn't report a version"
        return {
            'update_available': False,
            'current_version': None,
            'latest_version': None,
            'update_command': command,
            'message': f"yt-dlp isn't working ({reason}). Install it with: {command}",
        }

    latest_version = get_latest_version()
    if not latest_version:
        return {
            'update_available': False,
            'current_version': current_version,
            'latest_version': None,
            'update_command': command,
            'message': 'Could not check for yt-dlp updates (network unavailable?)',
        }

    has_update = is_update_available(current_version, latest_version)
    return {
        'update_available': has_update,
        'current_version': current_version,
        'latest_version': latest_version,
        'update_command': command,
        'message': (f"yt-dlp update available: {current_version} → {latest_version}"
                    if has_update else f"yt-dlp is up to date ({current_version})"),
    }


def notify_update_available(update_info: dict) -> None:
    """Display the result of check_ytdlp_updates()."""
    if not update_info:
        return
    if update_info['update_available']:
        Enhanced_Menu.print_section("yt-dlp UPDATE AVAILABLE")
        print(f"  Current version: {update_info['current_version']}")
        print(f"  Latest version:  {update_info['latest_version']}")
        print(f"\n  To update, run:\n    {update_info['update_command']}\n")
        log_manager.log_warning(update_info['message'], console=False)
    elif update_info['current_version']:
        Enhanced_Menu.print_status(update_info['message'],
                                   "success" if update_info['latest_version'] else "warning")
    else:
        Enhanced_Menu.print_status(update_info['message'], "error")


# ==================== Startup check ====================
def _state_file() -> Path:
    return Path(log_manager.HISTORY_DIR) / "ytdlp_update_check.json"


def check_on_startup(every_hours: float = CHECK_EVERY_HOURS) -> Optional[dict]:
    """
    For program start: check at most once per `every_hours` (or sooner if the
    installed version changed), and only print anything if there's an update.
    """
    try:
        last = json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = {}
    current = get_installed_version()
    fresh = time.time() - float(last.get("checked", 0)) < every_hours * 3600
    if fresh and last.get("current_version") == current:
        info = last.get("info")
    else:
        info = check_ytdlp_updates()
        if info['latest_version']:                    # only cache a successful check
            try:
                _state_file().parent.mkdir(parents=True, exist_ok=True)
                _state_file().write_text(json.dumps({"checked": time.time(),
                                                     "current_version": current,
                                                     "info": info}), encoding="utf-8")
            except OSError:
                pass
    if info and info.get('update_available'):
        notify_update_available(info)
    return info