"""Check for a newer yt-dlp, and update it, using tools/ytdlp_update_checker.py."""

import re
import shlex
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

from tools import ytdlp_update_checker as checker
from tools.dependency_check import find_program, run_program


@dataclass
class UpdateStatus:
    installed: Optional[str] = None
    latest: Optional[str] = None
    location: str = ""
    method: str = "unknown"
    command: Optional[List[str]] = None
    error: str = ""

    @property
    def outdated(self) -> bool:
        def parts(v):
            return tuple(int(p) for p in re.findall(r"\d+", v or ""))
        return bool(self.installed and self.latest and parts(self.installed) < parts(self.latest))


def _split_command(command: str) -> List[str]:
    """'"C:\\...\\yt-dlp.exe" -U' or 'py -m pip install -U yt-dlp' -> a list for run_program."""
    parts = shlex.split(command, posix=False)          # keeps Windows backslashes intact
    return [p[1:-1] if len(p) > 1 and p[0] == p[-1] == '"' else p for p in parts]


def _method(command: str) -> str:
    for word in ("pipx", "pip", "winget", "scoop", "brew"):
        if word in command:
            return word
    return "standalone" if command.rstrip().endswith("-U") else "unknown"


def check_ytdlp(online: bool = True) -> UpdateStatus:
    """Installed and latest versions, and the command that updates yt-dlp."""
    status = UpdateStatus()
    path, where = find_program("yt-dlp")
    status.installed = checker.get_installed_version()
    status.location = path or ""
    command = checker.update_command(path if status.installed else None)
    status.command = _split_command(command) if command else None
    status.method = _method(command or "")
    if not status.installed:
        status.error = f"yt-dlp isn't installed ({where})"
    if online:
        status.latest = checker.get_latest_version()
        if not status.latest:
            status.error = status.error or "Couldn't check for a newer version (no internet?)."
    return status


def update_ytdlp(status: UpdateStatus) -> Tuple[bool, str]:
    """Run the update command. (success, message)."""
    if not status.command:
        return False, "Couldn't tell how yt-dlp was installed; update it the same way you installed it."
    code, output = run_program(status.command, timeout=600)
    if code != 0:
        tail = "\n".join((output or "").strip().splitlines()[-5:])
        return False, f"Update failed:\n{tail}" if tail else "Update failed."
    after = checker.get_installed_version()
    if after and status.installed and after != status.installed:
        return True, f"Updated: {status.installed} → {after}"
    return (True, f"yt-dlp is now {after}.") if after else \
        (False, "The update ran, but yt-dlp still can't be found.")


class YtdlpUpdateWorker(QThread):
    checked = Signal(object)              # an UpdateStatus
    finished_update = Signal(bool, str)   # success, message

    def __init__(self, mode: str = "check", status=None):
        """mode 'check' looks online for the latest version; 'update' runs the update."""
        super().__init__()
        self.mode = mode
        self.status = status

    def run(self):
        if self.mode == "update":
            try:
                ok, message = update_ytdlp(self.status or check_ytdlp(online=False))
            except Exception as error:
                ok, message = False, f"Update failed: {type(error).__name__}: {error}"
            self.finished_update.emit(ok, message)
            return
        try:
            self.checked.emit(check_ytdlp(online=True))
        except Exception as error:
            self.checked.emit(UpdateStatus(error=f"Couldn't check: {error}"))