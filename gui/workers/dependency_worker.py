"""Run the dependency check (yt-dlp, ffmpeg, ...) without freezing the window."""

from PySide6.QtCore import QThread, Signal

from tools.dependency_check import check_dependencies


class DependencyWorker(QThread):
    done = Signal(object)              # a tools.dependency_check.DependencyReport

    def run(self):
        self.done.emit(check_dependencies())
