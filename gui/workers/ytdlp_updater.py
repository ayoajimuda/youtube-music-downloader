"""Check for a newer yt-dlp, and update it, using tools/ytdlp_update.py.

The update command depends on how yt-dlp was installed (pip, pipx, standalone
.exe, winget, scoop, brew); tools.ytdlp_update works that out, the same as the
terminal Tools menu.
"""

from PySide6.QtCore import QThread, Signal

from tools.ytdlp_update import check_for_update, run_update


class YtdlpUpdateWorker(QThread):
    checked = Signal(object)              # a tools.ytdlp_update.UpdateStatus
    finished_update = Signal(bool, str)   # success, message

    def __init__(self, mode: str = "check", status=None):
        """mode 'check' looks online for the latest version; 'update' runs the update."""
        super().__init__()
        self.mode = mode
        self.status = status

    def run(self):
        if self.mode == "update":
            try:
                status = self.status or check_for_update(online=False)
                ok, message = run_update(status)
            except Exception as error:
                ok, message = False, f"Update failed: {type(error).__name__}: {error}"
            self.finished_update.emit(ok, message)
            return
        try:
            self.checked.emit(check_for_update(online=True))
        except Exception as error:
            from tools.ytdlp_update import UpdateStatus
            self.checked.emit(UpdateStatus(error=f"Couldn't check: {error}"))
